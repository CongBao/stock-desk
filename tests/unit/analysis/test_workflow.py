from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, cast

from pydantic import JsonValue
import pytest

import scripts.check_import_boundaries as import_boundaries
from scripts.check_import_boundaries import find_import_boundary_violations
from stock_desk.analysis.evidence import (
    EvidenceGraph,
    EvidenceItem,
    EvidenceStance,
)
from stock_desk.analysis.providers.base import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ModelConnectionResult,
)
from stock_desk.analysis.roles import (
    ROLE_ORDER,
    RoleName,
    RoleOutputValidationError,
    load_role_prompt,
)
from stock_desk.analysis.snapshot import (
    ResearchQualityFlag,
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
)
from stock_desk.analysis.workflow import (
    AnalysisWorkflow,
    WorkflowRequestValidationError,
    WorkflowStageStatus,
)
from stock_desk.security.redaction import scoped_log_redaction


UTC = timezone.utc
FROZEN_AT = datetime(2025, 7, 6, 9, tzinfo=UTC)
FETCHED_AT = FROZEN_AT - timedelta(minutes=5)
DATA_CUTOFF = FETCHED_AT - timedelta(hours=1)
VERSION = "sha256:" + "a" * 64
SYMBOL = "600000.SH"
WORKFLOW_SECRET = "workflow-active-secret-must-not-escape"
SIMILAR_NON_SECRET = "workflow-active-secret-must-not-escapx"


def run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(awaitable)


def section(kind: ResearchSectionKind, *, value: str | None = None) -> ResearchSection:
    return ResearchSection(  # type: ignore[call-arg]
        kind=kind,
        canonical_source="fixture",
        source_record=f"{kind.value}:record-1",
        source_url=f"https://example.com/{kind.value}/record-1",
        published_at=(
            DATA_CUTOFF
            if kind in {ResearchSectionKind.ANNOUNCEMENTS, ResearchSectionKind.NEWS}
            else None
        ),
        data_cutoff=DATA_CUTOFF,
        fetched_at=FETCHED_AT,
        dataset_version=VERSION,
        quality_flags=(
            (ResearchQualityFlag.STALE,) if kind is ResearchSectionKind.NEWS else ()
        ),
        content={"kind": kind.value, "value": value or f"{kind.value}-fixture"},
    )


def snapshot(
    *, symbol: str = SYMBOL, market_value: str = "market-fixture"
) -> ResearchSnapshot:
    return ResearchSnapshot.create(
        symbol=symbol,
        frozen_at=FROZEN_AT,
        sections=(
            section(ResearchSectionKind.MARKET, value=market_value),
            section(ResearchSectionKind.FUNDAMENTALS),
            section(ResearchSectionKind.ANNOUNCEMENTS),
            section(ResearchSectionKind.NEWS),
        ),
        missing_sections=(),
    )


def evidence_graph(value: ResearchSnapshot) -> EvidenceGraph:
    items = tuple(
        EvidenceItem.create(
            snapshot=value,
            section_kind=item.kind,
            excerpt=json.dumps(
                item.content,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        for item in value.sections
    )
    return EvidenceGraph(snapshot=value, evidence_items=items, claims=())


def compact_evidence_graph(value: ResearchSnapshot) -> EvidenceGraph:
    return EvidenceGraph(
        snapshot=value,
        evidence_items=tuple(
            EvidenceItem.create(
                snapshot=value,
                section_kind=item.kind,
                excerpt=f"registered {item.kind.value} evidence",
            )
            for item in value.sections
        ),
        claims=(),
    )


def request_role(request: ModelRequest) -> RoleName:
    context = request.data_blocks[0]
    return RoleName(cast(str, context["role"]))


def allowed_evidence_ids(request: ModelRequest) -> tuple[str, ...]:
    context = request.data_blocks[0]
    raw = cast(list[JsonValue], context["allowed_evidence_ids"])
    return tuple(cast(str, item) for item in raw)


def valid_content(request: ModelRequest) -> dict[str, JsonValue]:
    role = request_role(request)
    evidence_ids = allowed_evidence_ids(request)
    assert evidence_ids
    content: dict[str, JsonValue] = {
        "role": role.value,
        "snapshot_id": cast(str, request.data_blocks[0]["snapshot_id"]),
        "summary": f"{role.value} summary",
        "claims": [
            {
                "text": f"{role.value} claim",
                "evidence_ids": [evidence_ids[0]],
                "stance": EvidenceStance.SUPPORT.value,
            }
        ],
    }
    if role is RoleName.RISK_DECISION:
        content["proposal"] = {
            "rating": "bullish",
            "confidence": 0.9,
            "confidence_explanation": "Registered evidence supports this confidence.",
        }
    return content


def response(
    request: ModelRequest,
    *,
    content: dict[str, JsonValue] | None = None,
) -> ModelResponse:
    return ModelResponse(
        provider="stub-provider",
        model="stub-model-v1",
        content=content or valid_content(request),
        usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
    )


class RecordingProvider:
    provider = "stub-provider"
    model = "configured-stub-model"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.events: list[tuple[str, RoleName]] = []
        self._analysts_started: set[RoleName] = set()
        self._analysts_ready = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        role = request_role(request)
        self.requests.append(request)
        self.events.append(("start", role))
        if role in {RoleName.TECHNICAL, RoleName.FUNDAMENTAL_NEWS}:
            self._analysts_started.add(role)
            if len(self._analysts_started) == 2:
                self._analysts_ready.set()
            await asyncio.wait_for(self._analysts_ready.wait(), timeout=1.0)
            await asyncio.sleep(0)
        self.events.append(("end", role))
        return response(request)

    async def test_connection(
        self, *, timeout_seconds: float = 10.0
    ) -> ModelConnectionResult:
        del timeout_seconds
        raise AssertionError("workflow must not test model connections")


def workflow(provider: RecordingProvider) -> AnalysisWorkflow:
    return AnalysisWorkflow(
        provider=provider,
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 7.0,
    )


def test_analysts_really_overlap_then_reviews_and_decision_follow_dependencies() -> (
    None
):
    frozen = snapshot()
    graph = evidence_graph(frozen)
    provider = RecordingProvider()

    result = run(workflow(provider).run(frozen, graph))

    technical_start = provider.events.index(("start", RoleName.TECHNICAL))
    fundamental_start = provider.events.index(("start", RoleName.FUNDAMENTAL_NEWS))
    first_analyst_end = min(
        provider.events.index(("end", RoleName.TECHNICAL)),
        provider.events.index(("end", RoleName.FUNDAMENTAL_NEWS)),
    )
    assert technical_start < first_analyst_end
    assert fundamental_start < first_analyst_end
    last_analyst_end = max(
        provider.events.index(("end", RoleName.TECHNICAL)),
        provider.events.index(("end", RoleName.FUNDAMENTAL_NEWS)),
    )
    assert provider.events.index(("start", RoleName.BULL)) > last_analyst_end
    assert provider.events.index(("start", RoleName.BEAR)) > last_analyst_end
    last_review_end = max(
        provider.events.index(("end", RoleName.BULL)),
        provider.events.index(("end", RoleName.BEAR)),
    )
    assert provider.events.index(("start", RoleName.RISK_DECISION)) > last_review_end
    assert tuple(item.role for item in result.outputs) == ROLE_ORDER
    assert result.snapshot_id == frozen.snapshot_id


def _blocks_for(
    provider: RecordingProvider, role: RoleName
) -> tuple[dict[str, JsonValue], ...]:
    request = next(item for item in provider.requests if request_role(item) is role)
    return request.data_blocks


def _untrusted_payloads(
    blocks: tuple[dict[str, JsonValue], ...],
) -> tuple[dict[str, JsonValue], ...]:
    return tuple(
        cast(dict[str, JsonValue], block["payload"])
        for block in blocks
        if block.get("trust_label") == "untrusted-data"
    )


def test_roles_receive_only_allowed_snapshot_and_structured_dependency_blocks() -> None:
    frozen = snapshot()
    provider = RecordingProvider()

    run(workflow(provider).run(frozen, evidence_graph(frozen)))

    technical = _blocks_for(provider, RoleName.TECHNICAL)
    fundamental = _blocks_for(provider, RoleName.FUNDAMENTAL_NEWS)
    technical_payloads = _untrusted_payloads(technical)
    fundamental_payloads = _untrusted_payloads(fundamental)
    assert {
        payload["section_kind"]
        for payload in technical_payloads
        if payload.get("data_kind") == "snapshot_section"
    } == {ResearchSectionKind.MARKET.value}
    assert {
        payload["section_kind"]
        for payload in fundamental_payloads
        if payload.get("data_kind") == "snapshot_section"
    } == {
        ResearchSectionKind.FUNDAMENTALS.value,
        ResearchSectionKind.ANNOUNCEMENTS.value,
        ResearchSectionKind.NEWS.value,
    }
    for role in (RoleName.BULL, RoleName.BEAR):
        blocks = _blocks_for(provider, role)
        payloads = _untrusted_payloads(blocks)
        assert not any(
            payload.get("data_kind") == "snapshot_section" for payload in payloads
        )
        assert {
            payload["role"]
            for payload in payloads
            if payload.get("data_kind") == "role_output"
        } == {RoleName.TECHNICAL.value, RoleName.FUNDAMENTAL_NEWS.value}
        assert any(
            payload.get("data_kind") == "evidence_reference" for payload in payloads
        )
    decision = _blocks_for(provider, RoleName.RISK_DECISION)
    decision_payloads = _untrusted_payloads(decision)
    assert not any(
        payload.get("data_kind") == "snapshot_section" for payload in decision_payloads
    )
    assert {
        payload["role"]
        for payload in decision_payloads
        if payload.get("data_kind") == "role_output"
    } == {RoleName.BULL.value, RoleName.BEAR.value}
    assert any(block.get("block_type") == "quality_flags" for block in decision)
    encoded = json.dumps(
        [request.data_blocks for request in provider.requests],
        ensure_ascii=False,
    ).lower()
    assert "formula" not in encoded
    assert "backtest" not in encoded
    assert "broker" not in encoded


def test_direct_workflow_cleans_secret_echo_before_dependency_requests() -> None:
    frozen = snapshot()

    class SecretEchoProvider(RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            result = await super().complete(request)
            if request_role(request) is not RoleName.TECHNICAL:
                return result
            content = result.content
            content["summary"] = (
                f"ordinary {WORKFLOW_SECRET}; similar {SIMILAR_NON_SECRET}; suffix"
            )
            claims = cast(list[dict[str, JsonValue]], content["claims"])
            claims[0]["text"] = (
                f"claim {WORKFLOW_SECRET}; similar {SIMILAR_NON_SECRET}; evidence"
            )
            return response(request, content=content)

    provider = SecretEchoProvider()
    with scoped_log_redaction(WORKFLOW_SECRET):
        result = run(workflow(provider).run(frozen, evidence_graph(frozen)))

    assert len(provider.requests) == 5
    request_payloads = tuple(request.model_dump_json() for request in provider.requests)
    assert all(WORKFLOW_SECRET not in payload for payload in request_payloads)
    review_payloads = tuple(
        payload
        for request, payload in zip(
            provider.requests,
            request_payloads,
            strict=True,
        )
        if request_role(request) in {RoleName.BULL, RoleName.BEAR}
    )
    assert len(review_payloads) == 2
    assert all(SIMILAR_NON_SECRET in payload for payload in review_payloads)
    result_payload = result.model_dump_json()
    assert WORKFLOW_SECRET not in result_payload
    assert SIMILAR_NON_SECRET in result_payload


def test_snapshot_and_registered_evidence_are_defensively_copied_between_roles() -> (
    None
):
    frozen = snapshot()
    graph = evidence_graph(frozen)

    class MutatingProvider(RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            result = await super().complete(request)
            if request_role(request) is RoleName.TECHNICAL:
                request.data_blocks[0]["snapshot_id"] = "mutated"
                for block in request.data_blocks:
                    if block.get("trust_label") != "untrusted-data":
                        continue
                    payload = cast(dict[str, JsonValue], block["payload"])
                    if payload.get("data_kind") == "snapshot_section":
                        cast(dict[str, JsonValue], payload["content"])["value"] = (
                            "mutated"
                        )
            return result

    provider = MutatingProvider()
    original_snapshot = frozen.canonical_json_bytes()
    original_graph = graph.model_dump_json()

    result = run(workflow(provider).run(frozen, graph))

    assert frozen.canonical_json_bytes() == original_snapshot
    assert graph.model_dump_json() == original_graph
    assert result.snapshot_id == frozen.snapshot_id
    review_context = _blocks_for(provider, RoleName.BULL)[0]
    assert review_context["snapshot_id"] == frozen.snapshot_id


def test_oversized_role_request_fails_before_any_provider_call() -> None:
    frozen = snapshot(market_value="x" * 261_900)
    provider = RecordingProvider()

    with pytest.raises(WorkflowRequestValidationError):
        run(workflow(provider).run(frozen, compact_evidence_graph(frozen)))

    assert provider.requests == []


def test_trace_is_canonical_complete_and_deterministic_with_fixed_collaborators() -> (
    None
):
    frozen = snapshot()
    graph = evidence_graph(frozen)

    first = run(workflow(RecordingProvider()).run(frozen, graph))
    second = run(workflow(RecordingProvider()).run(frozen, graph))

    assert first == second
    assert tuple(item.role for item in first.trace) == ROLE_ORDER
    assert all(item.status is WorkflowStageStatus.SUCCEEDED for item in first.trace)
    assert all(item.started_at == FROZEN_AT for item in first.trace)
    assert all(item.ended_at == FROZEN_AT for item in first.trace)
    assert all(item.duration_seconds == 0.0 for item in first.trace)
    assert all(item.provider == "stub-provider" for item in first.trace)
    assert all(item.model == "stub-model-v1" for item in first.trace)
    assert tuple(item.template_version for item in first.trace) == (
        "technical-v1",
        "fundamental_news-v1",
        "bull-v1",
        "bear-v1",
        "risk_decision-v2",
    )
    assert all(item.template_hash.startswith("sha256:") for item in first.trace)
    assert all(item.request_hash.startswith("sha256:") for item in first.trace)
    assert first == type(first).model_validate_json(first.model_dump_json())


def test_prompt_is_read_once_and_trace_hash_matches_exact_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read_bytes = Path.read_bytes
    technical_reads = 0

    def drifting_read_bytes(path: Path) -> bytes:
        nonlocal technical_reads
        raw = original_read_bytes(path)
        if path.name != "technical.md":
            return raw
        technical_reads += 1
        if technical_reads == 2:
            return raw[:-1] + b"\nDrifted instruction.\n"
        return raw

    monkeypatch.setattr(Path, "read_bytes", drifting_read_bytes)
    frozen = snapshot()
    provider = RecordingProvider()

    result = run(workflow(provider).run(frozen, evidence_graph(frozen)))

    assert technical_reads == 1
    request = next(
        item for item in provider.requests if request_role(item) is RoleName.TECHNICAL
    )
    trace = next(item for item in result.trace if item.role is RoleName.TECHNICAL)
    assert trace.template_hash == (
        "sha256:" + hashlib.sha256(request.system.encode("utf-8")).hexdigest()
    )
    assert trace.request_hash == request.stable_hash()


def test_graph_must_be_the_exact_registered_snapshot() -> None:
    frozen = snapshot()
    other = snapshot(symbol="000001.SZ")
    provider = RecordingProvider()

    with pytest.raises(ValueError, match="snapshot"):
        run(workflow(provider).run(frozen, evidence_graph(other)))

    assert provider.requests == []


@pytest.mark.parametrize("failure_kind", ["unknown", "duplicate", "cross_snapshot"])
def test_role_output_evidence_references_fail_closed(failure_kind: str) -> None:
    frozen = snapshot()
    graph = evidence_graph(frozen)
    other_graph = evidence_graph(snapshot(symbol="000001.SZ"))

    class InvalidEvidenceProvider(RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            role = request_role(request)
            if role is not RoleName.TECHNICAL:
                return await super().complete(request)
            content = valid_content(request)
            claim = cast(list[dict[str, JsonValue]], content["claims"])[0]
            if failure_kind == "unknown":
                claim["evidence_ids"] = ["sha256:" + "f" * 64]
            elif failure_kind == "duplicate":
                evidence_id = allowed_evidence_ids(request)[0]
                claim["evidence_ids"] = [evidence_id, evidence_id]
            else:
                claim["evidence_ids"] = [other_graph.evidence_items[0].evidence_id]
            return response(request, content=content)

    with pytest.raises(RoleOutputValidationError):
        run(workflow(InvalidEvidenceProvider()).run(frozen, graph))


def _replace_with_many_claims(content: dict[str, JsonValue]) -> None:
    claims = cast(list[dict[str, JsonValue]], content["claims"])
    evidence_ids = claims[0]["evidence_ids"]
    content["claims"] = [
        {
            "text": f"claim-{index}",
            "evidence_ids": evidence_ids,
            "stance": EvidenceStance.SUPPORT.value,
        }
        for index in range(17)
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda content: content.update(role=RoleName.BEAR.value),
        lambda content: content.update(snapshot_id="sha256:" + "f" * 64),
        lambda content: content.update(unexpected="unsafe"),
        lambda content: content.update(summary="x" * 70_000),
        lambda content: _replace_with_many_claims(content),
    ],
    ids=["wrong-role", "wrong-snapshot", "extra-field", "byte-limit", "claim-limit"],
)
def test_role_output_schema_and_complexity_fail_closed(
    mutate: Callable[[dict[str, JsonValue]], None],
) -> None:
    frozen = snapshot()

    class InvalidOutputProvider(RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request_role(request) is not RoleName.TECHNICAL:
                return await super().complete(request)
            content = valid_content(request)
            mutate(content)
            return response(request, content=content)

    with pytest.raises(RoleOutputValidationError):
        run(workflow(InvalidOutputProvider()).run(frozen, evidence_graph(frozen)))


def test_parallel_failure_cancels_and_waits_for_sibling_without_starting_reviews() -> (
    None
):
    frozen = snapshot()
    original = RuntimeError("original")

    class FailingProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.fundamental_cancelled = asyncio.Event()

        async def complete(self, request: ModelRequest) -> ModelResponse:
            role = request_role(request)
            self.requests.append(request)
            self.events.append(("start", role))
            if role is RoleName.TECHNICAL:
                await asyncio.sleep(0)
                raise original
            if role is RoleName.FUNDAMENTAL_NEWS:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.fundamental_cancelled.set()
                    raise
            raise AssertionError("reviews must not start after analyst failure")

    provider = FailingProvider()

    with pytest.raises(RuntimeError) as captured:
        run(workflow(provider).run(frozen, evidence_graph(frozen)))

    assert captured.value is original
    assert provider.fundamental_cancelled.is_set()
    assert {request_role(item) for item in provider.requests} == {
        RoleName.TECHNICAL,
        RoleName.FUNDAMENTAL_NEWS,
    }


def test_self_cancelled_role_cancels_and_waits_for_blocking_sibling() -> None:
    frozen = snapshot()

    class SelfCancellingProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.fundamental_cancelled = asyncio.Event()

        async def complete(self, request: ModelRequest) -> ModelResponse:
            role = request_role(request)
            self.requests.append(request)
            if len(self.requests) == 2:
                self.started.set()
            await self.started.wait()
            if role is RoleName.TECHNICAL:
                raise asyncio.CancelledError()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.fundamental_cancelled.set()
                raise
            raise AssertionError("blocking future unexpectedly completed")

    async def scenario() -> bool:
        provider = SelfCancellingProvider()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                workflow(provider).run(frozen, evidence_graph(frozen)),
                timeout=0.2,
            )
        leaked = [
            child
            for child in asyncio.all_tasks()
            if child is not asyncio.current_task()
            and child.get_name().startswith("analysis-role-")
            and not child.done()
        ]
        assert leaked == []
        return provider.fundamental_cancelled.is_set()

    assert run(scenario())


def test_caller_cancellation_cancels_and_waits_for_all_parallel_roles() -> None:
    frozen = snapshot()

    class BlockingProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled: set[RoleName] = set()

        async def complete(self, request: ModelRequest) -> ModelResponse:
            role = request_role(request)
            self.requests.append(request)
            if len(self.requests) == 2:
                self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.add(role)
                raise
            raise AssertionError("blocking future unexpectedly completed")

    async def scenario() -> set[RoleName]:
        provider = BlockingProvider()
        task = asyncio.create_task(
            workflow(provider).run(frozen, evidence_graph(frozen)),
            name="test-workflow-parent",
        )
        await asyncio.wait_for(provider.started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        leaked = [
            child
            for child in asyncio.all_tasks()
            if child is not asyncio.current_task()
            and child.get_name().startswith("analysis-role-")
            and not child.done()
        ]
        assert leaked == []
        return provider.cancelled

    assert run(scenario()) == {RoleName.TECHNICAL, RoleName.FUNDAMENTAL_NEWS}


def test_prompt_templates_have_stable_versions_hashes_and_safety_rules() -> None:
    forbidden_outputs = (
        "target price",
        "position sizing",
        "personalized investment advice",
        "place orders",
        "unsupported claims",
        "formula",
        "backtest",
        "external text",
    )

    for role in ROLE_ORDER:
        first = load_role_prompt(role)
        second = load_role_prompt(role)
        normalized = first.content.lower()
        assert first == second
        assert first.role is role
        expected_version = (
            "risk_decision-v2" if role is RoleName.RISK_DECISION else f"{role.value}-v1"
        )
        assert first.version == expected_version
        assert first.content_hash == (
            "sha256:" + hashlib.sha256(first.content.encode("utf-8")).hexdigest()
        )
        assert len(first.content_hash) == 71
        assert all(rule in normalized for rule in forbidden_outputs)


def test_analysis_import_boundary_rejects_domain_and_direct_network_imports(
    tmp_path: Path,
) -> None:
    clean_root = Path(__file__).resolve().parents[3] / "src/stock_desk/analysis"
    assert find_import_boundary_violations(clean_root) == ()

    analysis_root = tmp_path / "analysis"
    analysis_root.mkdir()
    (analysis_root / "bad.py").write_text(
        "import stock_desk.formula\nfrom stock_desk.backtest import service\n",
        encoding="utf-8",
    )
    (analysis_root / "workflow.py").write_text(
        "import httpx2\nimport akshare\n",
        encoding="utf-8",
    )
    (analysis_root / "dynamic.py").write_text(
        'import importlib\nimportlib.import_module("stock_desk.formula.service")\n'
        '__import__("stock_desk.broker")\n',
        encoding="utf-8",
    )
    (analysis_root / "relative.py").write_text(
        "from ..formula import service\n",
        encoding="utf-8",
    )

    violations = find_import_boundary_violations(analysis_root)

    assert violations == (
        "bad.py:1: forbidden analysis dependency stock_desk.formula",
        "bad.py:2: forbidden analysis dependency stock_desk.backtest",
        "dynamic.py:2: forbidden analysis dependency stock_desk.formula.service",
        "dynamic.py:3: forbidden analysis dependency stock_desk.broker",
        "relative.py:1: forbidden analysis dependency stock_desk.formula",
        "workflow.py:1: forbidden workflow runtime dependency httpx2",
        "workflow.py:2: forbidden workflow runtime dependency akshare",
    )


def test_analysis_api_import_boundary_rejects_formula_and_backtest(
    tmp_path: Path,
) -> None:
    api_path = Path(__file__).resolve().parents[3] / "src/stock_desk/api/analysis.py"
    assert import_boundaries.find_analysis_api_boundary_violations(api_path) == ()

    bad_api_path = tmp_path / "analysis.py"
    bad_api_path.write_text(
        "import stock_desk.formula\nfrom stock_desk.backtest import service\n",
        encoding="utf-8",
    )

    assert import_boundaries.find_analysis_api_boundary_violations(bad_api_path) == (
        "analysis.py:1: forbidden analysis API dependency stock_desk.formula",
        "analysis.py:2: forbidden analysis API dependency stock_desk.backtest",
    )


def test_importing_workflow_does_not_eagerly_load_forbidden_domains() -> None:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import stock_desk.analysis.roles; "
                "import stock_desk.analysis.workflow; "
                "forbidden=('stock_desk.formula','stock_desk.backtest',"
                "'stock_desk.broker'); "
                "loaded=[name for name in sys.modules if any("
                "name == item or name.startswith(item + '.') for item in forbidden)]; "
                "raise SystemExit(1 if loaded else 0)"
            ),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
