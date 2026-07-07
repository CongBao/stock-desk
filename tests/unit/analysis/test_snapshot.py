from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from typing import cast

from pydantic import ValidationError
import pytest

from stock_desk.analysis import snapshot as snapshot_module
from stock_desk.analysis.data_service import (
    ResearchDataService,
    ResearchDataUnavailable,
)
from stock_desk.analysis.snapshot import (
    MAX_SECTION_CONTENT_BYTES,
    MAX_SECTION_CONTENT_DEPTH,
    MAX_SECTION_CONTENT_NODES,
    MissingResearchSection,
    ResearchMissingReason,
    ResearchQualityFlag,
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
    ResearchSnapshotBuilder,
)


UTC = timezone.utc
FROZEN_AT = datetime(2025, 7, 6, 9, tzinfo=UTC)
FETCHED_AT = FROZEN_AT - timedelta(minutes=5)
DATA_CUTOFF = FETCHED_AT - timedelta(hours=1)
DIGEST = "sha256:" + "a" * 64
SYMBOL = "600000.SH"
SECTION_ORDER = (
    ResearchSectionKind.MARKET,
    ResearchSectionKind.FUNDAMENTALS,
    ResearchSectionKind.ANNOUNCEMENTS,
    ResearchSectionKind.NEWS,
)


def _section(
    kind: ResearchSectionKind,
    *,
    content: dict[str, object] | None = None,
    dataset_version: str = DIGEST,
    data_cutoff: datetime = DATA_CUTOFF,
    fetched_at: datetime = FETCHED_AT,
    published_at: datetime | None = DATA_CUTOFF,
    source_url: str | None = "https://example.com/source/record-1",
    quality_flags: tuple[ResearchQualityFlag, ...] = (),
) -> ResearchSection:
    return ResearchSection(
        kind=kind,
        canonical_source="tushare",
        source_record=f"{kind.value}:record-1",
        source_url=source_url,
        published_at=published_at,
        data_cutoff=data_cutoff,
        fetched_at=fetched_at,
        dataset_version=dataset_version,
        quality_flags=quality_flags,
        content=cast(
            dict[str, object],
            content
            if content is not None
            else {"kind": kind.value, "value": "fixture"},
        ),
    )


class StubLoader:
    def __init__(
        self,
        kind: ResearchSectionKind,
        outcome: ResearchSection | Exception,
        calls: list[ResearchSectionKind],
    ) -> None:
        self.kind = kind
        self._outcome = outcome
        self._calls = calls

    def load(self, symbol: str) -> ResearchSection:
        assert symbol == SYMBOL
        self._calls.append(self.kind)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _service(
    *,
    order: tuple[ResearchSectionKind, ...] = SECTION_ORDER,
    overrides: dict[ResearchSectionKind, ResearchSection | Exception] | None = None,
    calls: list[ResearchSectionKind] | None = None,
    clock: Callable[[], datetime] = lambda: FROZEN_AT,
) -> ResearchDataService:
    trace = calls if calls is not None else []
    values = overrides or {}
    return ResearchDataService(
        loaders=tuple(
            StubLoader(kind, values.get(kind, _section(kind)), trace) for kind in order
        ),
        clock=clock,
    )


def _snapshot(
    *,
    service: ResearchDataService | None = None,
    clock: Callable[[], datetime] = lambda: FROZEN_AT,
) -> ResearchSnapshot:
    return ResearchSnapshotBuilder(
        data_service=service or _service(),
        clock=clock,
    ).build(SYMBOL)


def test_builder_freezes_all_four_sections_and_cutoffs() -> None:
    snapshot = _snapshot()

    assert snapshot.symbol == SYMBOL
    assert snapshot.frozen_at == FROZEN_AT
    assert tuple(section.kind for section in snapshot.sections) == SECTION_ORDER
    assert snapshot.missing_sections == ()
    assert all(section.data_cutoff == DATA_CUTOFF for section in snapshot.sections)
    assert all(section.fetched_at == FETCHED_AT for section in snapshot.sections)
    assert all(section.dataset_version == DIGEST for section in snapshot.sections)
    assert snapshot.snapshot_id.startswith("sha256:")
    assert len(snapshot.snapshot_id) == 71


def test_snapshot_identity_is_stable_across_loader_order_and_sensitive_to_data() -> (
    None
):
    forward = _snapshot(service=_service(order=SECTION_ORDER))
    reverse = _snapshot(service=_service(order=tuple(reversed(SECTION_ORDER))))
    changed_content = _snapshot(
        service=_service(
            overrides={
                ResearchSectionKind.MARKET: _section(
                    ResearchSectionKind.MARKET,
                    content={"kind": "market", "value": "changed"},
                )
            }
        )
    )
    changed_provenance = _snapshot(
        service=_service(
            overrides={
                ResearchSectionKind.MARKET: _section(
                    ResearchSectionKind.MARKET,
                    dataset_version="sha256:" + "b" * 64,
                )
            }
        )
    )

    assert forward.snapshot_id == reverse.snapshot_id
    assert forward.canonical_json_bytes() == reverse.canonical_json_bytes()
    assert changed_content.snapshot_id != forward.snapshot_id
    assert changed_provenance.snapshot_id != forward.snapshot_id


def test_snapshot_content_is_deeply_immutable_and_defensively_copied() -> None:
    original = {"items": [{"value": 1}], "labels": ["a", "b"]}
    market = _section(ResearchSectionKind.MARKET, content=original)
    snapshot = _snapshot(
        service=_service(overrides={ResearchSectionKind.MARKET: market})
    )

    original["items"][0]["value"] = 99  # type: ignore[index]
    first = snapshot.section(ResearchSectionKind.MARKET)
    assert first is not None
    exposed = first.content
    exposed["items"][0]["value"] = 77  # type: ignore[index]

    assert first.content["items"][0]["value"] == 1  # type: ignore[index]
    with pytest.raises(ValidationError, match="frozen"):
        first.dataset_version = "sha256:" + "f" * 64
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.symbol = "000001.SZ"


def test_section_serialization_exposes_content_not_internal_bytes_name() -> None:
    section = _section(ResearchSectionKind.MARKET)

    payload = section.model_dump(mode="json")

    assert payload["content"] == {"kind": "market", "value": "fixture"}
    assert "content_json" not in payload


def test_section_and_snapshot_json_round_trip_with_object_schema() -> None:
    section = _section(
        ResearchSectionKind.MARKET,
        quality_flags=(ResearchQualityFlag.STALE,),
    )
    snapshot = _snapshot(
        service=_service(overrides={ResearchSectionKind.MARKET: section})
    )

    restored_section = ResearchSection.model_validate_json(
        section.model_dump_json(by_alias=True)
    )
    restored_snapshot = ResearchSnapshot.model_validate_json(
        snapshot.model_dump_json(by_alias=True)
    )
    schema = ResearchSection.model_json_schema(by_alias=True)

    assert restored_section == section
    assert restored_snapshot == snapshot
    assert schema["properties"]["content"]["type"] == "object"
    assert "format" not in schema["properties"]["content"]
    assert "content_json" not in schema["properties"]


def test_missing_section_is_explicit_and_never_an_empty_success() -> None:
    unavailable = ResearchDataUnavailable(
        kind=ResearchSectionKind.NEWS,
        reason=ResearchMissingReason.PERMISSION_DENIED,
        attempted_sources=("tushare", "akshare"),
        unsafe_context="token=TOP-SECRET",
    )
    snapshot = _snapshot(
        service=_service(overrides={ResearchSectionKind.NEWS: unavailable})
    )

    assert snapshot.section(ResearchSectionKind.NEWS) is None
    assert len(snapshot.missing_sections) == 1
    missing = snapshot.missing_sections[0]
    assert missing.kind is ResearchSectionKind.NEWS
    assert missing.reason is ResearchMissingReason.PERMISSION_DENIED
    assert missing.attempted_sources == ("tushare", "akshare")
    assert missing.checked_at == FROZEN_AT
    assert "TOP-SECRET" not in str(unavailable)
    assert "TOP-SECRET" not in repr(unavailable)


def test_snapshot_requires_exactly_one_outcome_for_every_kind() -> None:
    sections = tuple(_section(kind) for kind in SECTION_ORDER)
    complete = ResearchSnapshot.create(
        symbol=SYMBOL,
        frozen_at=FROZEN_AT,
        sections=sections,
        missing_sections=(),
    )

    with pytest.raises(ValidationError, match="exactly one outcome"):
        ResearchSnapshot.create(
            symbol=SYMBOL,
            frozen_at=FROZEN_AT,
            sections=sections[:-1],
            missing_sections=(),
        )
    with pytest.raises(ValidationError, match="exactly one outcome"):
        ResearchSnapshot.create(
            symbol=SYMBOL,
            frozen_at=FROZEN_AT,
            sections=sections + (sections[0],),
            missing_sections=(),
        )
    with pytest.raises(ValidationError, match="exactly one outcome"):
        ResearchSnapshot.create(
            symbol=SYMBOL,
            frozen_at=FROZEN_AT,
            sections=sections,
            missing_sections=(
                MissingResearchSection(
                    kind=ResearchSectionKind.NEWS,
                    reason=ResearchMissingReason.NO_DATA,
                    checked_at=FROZEN_AT,
                    attempted_sources=("tushare",),
                    recovery_code="refresh_source_data",
                ),
            ),
        )

    forged = complete.model_copy(update={"snapshot_id": "sha256:" + "0" * 64})
    with pytest.raises(ValidationError, match="snapshot_id"):
        ResearchSnapshot.model_validate(forged.model_dump(mode="python"))


def test_snapshot_schema_version_is_fixed() -> None:
    snapshot = _snapshot()
    payload = snapshot.model_dump(mode="python")
    payload["schema_version"] = "analysis-snapshot-v999"

    with pytest.raises(ValidationError, match="schema_version"):
        ResearchSnapshot.model_validate(payload)


def test_data_service_rejects_loader_kind_mismatch_and_unknown_errors() -> None:
    mismatch = _service(
        overrides={
            ResearchSectionKind.MARKET: _section(ResearchSectionKind.NEWS),
        }
    )
    with pytest.raises(ValueError, match="loader kind"):
        mismatch.load_all(SYMBOL)

    broken = _service(
        overrides={ResearchSectionKind.MARKET: RuntimeError("programming bug")}
    )
    with pytest.raises(RuntimeError, match="programming bug"):
        broken.load_all(SYMBOL)


def test_data_service_public_mismatch_error_clears_adapter_secret_chain() -> None:
    secret = "PROVIDER-SECRET-CAUSE"

    class ChainedFailureLoader:
        kind = ResearchSectionKind.MARKET

        def load(self, _symbol: str) -> ResearchSection:
            try:
                raise RuntimeError(secret)
            except RuntimeError as error:
                raise ResearchDataUnavailable(
                    kind=ResearchSectionKind.NEWS,
                    reason=ResearchMissingReason.INVALID_RESPONSE,
                    attempted_sources=("tushare",),
                ) from error

    service = ResearchDataService(
        loaders=(ChainedFailureLoader(),),
        clock=lambda: FROZEN_AT,
    )

    with pytest.raises(ValueError, match="failure kind") as captured:
        service.load_all(SYMBOL)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered = f"{captured.value!s} {captured.value!r}"
    assert secret not in rendered


def test_data_service_uses_fixed_category_order_even_if_registered_reversed() -> None:
    calls: list[ResearchSectionKind] = []
    outcomes = _service(order=tuple(reversed(SECTION_ORDER)), calls=calls).load_all(
        SYMBOL
    )

    assert calls == list(SECTION_ORDER)
    assert tuple(outcome.kind for outcome in outcomes) == SECTION_ORDER


def test_data_service_reports_unregistered_loader_as_no_provider() -> None:
    service = _service(order=SECTION_ORDER[:-1])

    outcomes = service.load_all(SYMBOL)

    missing = outcomes[-1]
    assert isinstance(missing, MissingResearchSection)
    assert missing.kind is ResearchSectionKind.NEWS
    assert missing.reason is ResearchMissingReason.NO_PROVIDER
    assert missing.attempted_sources == ()


@pytest.mark.parametrize(
    ("kind", "published_at"),
    [
        (ResearchSectionKind.NEWS, None),
        (ResearchSectionKind.ANNOUNCEMENTS, None),
    ],
)
def test_published_sections_require_publication_time(
    kind: ResearchSectionKind,
    published_at: datetime | None,
) -> None:
    with pytest.raises(ValidationError, match="publication"):
        _section(kind, published_at=published_at)


@pytest.mark.parametrize(
    "updates",
    [
        {"data_cutoff": FETCHED_AT + timedelta(seconds=1)},
        {"published_at": FETCHED_AT + timedelta(seconds=1)},
        {"fetched_at": FETCHED_AT.replace(tzinfo=None)},
    ],
)
def test_section_rejects_invalid_or_naive_times(updates: dict[str, object]) -> None:
    values: dict[str, object] = {
        "data_cutoff": DATA_CUTOFF,
        "fetched_at": FETCHED_AT,
        "published_at": DATA_CUTOFF,
    }
    values.update(updates)
    with pytest.raises(ValidationError):
        _section(
            ResearchSectionKind.NEWS,
            data_cutoff=cast(datetime, values["data_cutoff"]),
            fetched_at=cast(datetime, values["fetched_at"]),
            published_at=cast(datetime | None, values["published_at"]),
        )


def test_snapshot_rejects_section_or_missing_check_after_freeze() -> None:
    future_section = _section(
        ResearchSectionKind.MARKET,
        data_cutoff=FROZEN_AT,
        fetched_at=FROZEN_AT + timedelta(seconds=1),
        published_at=FROZEN_AT,
    )
    with pytest.raises(ValidationError, match="freeze"):
        ResearchSnapshot.create(
            symbol=SYMBOL,
            frozen_at=FROZEN_AT,
            sections=(future_section,)
            + tuple(_section(kind) for kind in SECTION_ORDER[1:]),
            missing_sections=(),
        )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/source",
        "https://user:password@example.com/source",
        "https://example.com/source\nheader: injected",
    ],
)
def test_source_url_rejects_unsafe_values(url: str) -> None:
    with pytest.raises(ValidationError, match="URL"):
        _section(ResearchSectionKind.MARKET, source_url=url)


def test_section_content_enforces_byte_depth_node_and_finite_budgets() -> None:
    with pytest.raises(ValueError, match="byte limit"):
        _section(
            ResearchSectionKind.MARKET,
            content={"payload": "x" * (MAX_SECTION_CONTENT_BYTES + 1)},
        )

    nested: dict[str, object] = {"leaf": True}
    for _ in range(MAX_SECTION_CONTENT_DEPTH + 1):
        nested = {"nested": nested}
    with pytest.raises(ValueError, match="depth limit"):
        _section(ResearchSectionKind.MARKET, content=nested)

    with pytest.raises(ValueError, match="node limit"):
        _section(
            ResearchSectionKind.MARKET,
            content={"items": list(range(MAX_SECTION_CONTENT_NODES + 1))},
        )

    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="finite"):
            _section(ResearchSectionKind.MARKET, content={"value": value})


def test_oversized_raw_content_bytes_are_rejected_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _section(ResearchSectionKind.MARKET).model_dump(mode="python")
    payload["content"] = b"{" + b" " * MAX_SECTION_CONTENT_BYTES + b"}"

    def fail_decode(_value: object) -> object:
        pytest.fail("oversized raw bytes reached json.loads")

    monkeypatch.setattr(snapshot_module.json, "loads", fail_decode)

    with pytest.raises(ValidationError, match="byte limit"):
        ResearchSection.model_validate(payload)


def test_quality_flags_are_unique_and_canonical() -> None:
    section = _section(
        ResearchSectionKind.MARKET,
        quality_flags=(
            ResearchQualityFlag.STALE,
            ResearchQualityFlag.DEGRADED_SOURCE,
        ),
    )
    assert section.quality_flags == (
        ResearchQualityFlag.DEGRADED_SOURCE,
        ResearchQualityFlag.STALE,
    )

    with pytest.raises(ValidationError, match="duplicate"):
        _section(
            ResearchSectionKind.MARKET,
            quality_flags=(
                ResearchQualityFlag.STALE,
                ResearchQualityFlag.STALE,
            ),
        )


def test_analysis_snapshot_modules_do_not_import_formula_or_backtest() -> None:
    project_root = Path(__file__).resolve().parents[3]
    forbidden = {"stock_desk.formula", "stock_desk.backtest"}
    violations: list[str] = []
    for relative in (
        "src/stock_desk/analysis/snapshot.py",
        "src/stock_desk/analysis/evidence.py",
        "src/stock_desk/analysis/data_service.py",
    ):
        tree = ast.parse((project_root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: tuple[str, ...]
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = (node.module,)
            else:
                continue
            for name in names:
                if any(
                    name == item or name.startswith(f"{item}.") for item in forbidden
                ):
                    violations.append(f"{relative}:{node.lineno}:{name}")

    assert violations == []
