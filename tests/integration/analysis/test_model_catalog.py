from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from stock_desk.analysis.model_catalog import (
    AnalysisModelCatalog,
    ModelConfigListKey,
    ModelCatalogClosed,
    ModelCatalogConflict,
    ModelCatalogCorruption,
    ModelConfigStatus,
    ModelNotFound,
    ModelNotVerified,
)
from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    ModelProviderKind,
)
from stock_desk.storage.database import create_engine_for_url, migrate


NOW = datetime(2026, 7, 7, 9, 0, tzinfo=timezone.utc)


def _config(*, model: str = "qwen3:8b") -> AnalysisModelPublicConfig:
    return AnalysisModelPublicConfig(
        provider=ModelProviderKind.OLLAMA,
        base_url="http://127.0.0.1:11434",
        model=model,
        temperature=0.1,
        timeout_seconds=90.0,
        max_output_tokens=4096,
    )


@pytest.fixture
def catalog(tmp_path: Path) -> AnalysisModelCatalog:
    url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(url)
    value = AnalysisModelCatalog(create_engine_for_url(url), clock=lambda: NOW)
    try:
        yield value
    finally:
        value.close()


def test_create_is_content_addressed_and_public_snapshot_repr_is_safe(
    catalog: AnalysisModelCatalog,
) -> None:
    public_config = _config()
    saved = catalog.create(display_name="Local model", public_config=public_config)
    canonical = json.dumps(
        public_config.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected_id = f"sha256:{hashlib.sha256(canonical.encode('ascii')).hexdigest()}"

    assert saved.id == saved.public_config_hash == expected_id
    assert saved.status is ModelConfigStatus.UNVERIFIED
    assert saved.revision == 0
    assert saved.provider is ModelProviderKind.OLLAMA
    assert saved.model == "qwen3:8b"
    assert saved.api_key_configured is False
    assert "secret_reference" not in repr(saved)
    assert not hasattr(saved, "secret_reference_id")


def test_plus_eight_clock_is_stored_and_read_as_same_utc_instant(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'timezone.db'}"
    migrate(url)
    plus_eight = timezone(timedelta(hours=8))
    catalog = AnalysisModelCatalog(
        create_engine_for_url(url),
        clock=lambda: datetime(2026, 7, 7, 9, 0, tzinfo=plus_eight),
    )
    try:
        saved = catalog.create(display_name="Local", public_config=_config())
        with catalog.engine.connect() as connection:
            raw = connection.exec_driver_sql(
                "SELECT created_at FROM analysis_model_config WHERE id=?",
                (saved.id,),
            ).scalar_one()

        assert saved.created_at == datetime(2026, 7, 7, 1, 0, tzinfo=timezone.utc)
        assert saved.updated_at == saved.created_at
        assert raw == "2026-07-07 01:00:00.000000"
    finally:
        catalog.close()


def test_content_hash_matches_analysis_run_canonical_utf8_json(
    catalog: AnalysisModelCatalog,
) -> None:
    public_config = _config(model="本地模型")
    saved = catalog.create(display_name="Unicode model", public_config=public_config)
    canonical = json.dumps(
        public_config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    assert saved.id == f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def test_successor_preserves_old_config_bytes_and_links_versions(
    catalog: AnalysisModelCatalog,
) -> None:
    first = catalog.create(display_name="Local", public_config=_config())
    with catalog.engine.connect() as connection:
        before = connection.exec_driver_sql(
            "SELECT public_config_json FROM analysis_model_config WHERE id=?",
            (first.id,),
        ).scalar_one()

    second = catalog.create_successor(
        first.id,
        display_name="Local v2",
        public_config=_config(model="qwen3:14b"),
    )

    with catalog.engine.connect() as connection:
        after = connection.exec_driver_sql(
            "SELECT public_config_json FROM analysis_model_config WHERE id=?",
            (first.id,),
        ).scalar_one()
    assert second.supersedes_id == first.id
    assert second.id != first.id
    assert after == before


def test_display_name_is_safely_mutable_but_execution_fields_are_not(
    catalog: AnalysisModelCatalog,
) -> None:
    saved = catalog.create(display_name="Local", public_config=_config())

    renamed = catalog.update_display_name(
        saved.id, "Renamed local", expected_revision=saved.revision
    )

    assert renamed.display_name == "Renamed local"
    assert renamed.id == saved.id
    assert renamed.public_config_hash == saved.public_config_hash
    assert renamed.revision == 1
    with pytest.raises(ValueError):
        catalog.update_display_name(
            saved.id, " unsafe ", expected_revision=renamed.revision
        )


def test_connection_test_transitions_use_cas_and_only_verified_enabled_models_run(
    catalog: AnalysisModelCatalog,
) -> None:
    saved = catalog.create(display_name="Local", public_config=_config())

    with pytest.raises(ModelNotVerified):
        catalog.require_verified(saved.id)
    failed = catalog.mark_test_result(
        saved.id,
        expected_status=ModelConfigStatus.UNVERIFIED,
        expected_revision=saved.revision,
        succeeded=False,
        error_code="connection_failed",
    )
    assert failed.status is ModelConfigStatus.FAILED
    assert failed.verified_at is None
    assert failed.last_tested_at == NOW + timedelta(microseconds=1)
    assert failed.updated_at == NOW + timedelta(microseconds=1)
    assert failed.error_code == "connection_failed"
    assert failed.revision == 1
    with pytest.raises(ModelCatalogConflict):
        catalog.mark_test_result(
            saved.id,
            expected_status=ModelConfigStatus.UNVERIFIED,
            expected_revision=saved.revision,
            succeeded=True,
        )

    verified = catalog.mark_test_result(
        saved.id,
        expected_status=ModelConfigStatus.FAILED,
        expected_revision=failed.revision,
        succeeded=True,
    )
    execution = catalog.require_verified(saved.id)
    assert verified.status is ModelConfigStatus.VERIFIED
    assert verified.verified_at == NOW + timedelta(microseconds=2)
    assert verified.last_tested_at == NOW + timedelta(microseconds=2)
    assert verified.updated_at == NOW + timedelta(microseconds=2)
    assert verified.error_code is None
    assert verified.revision == 2
    assert execution.model_config_id == saved.public_config_hash
    assert execution.public_config == _config()
    assert "secret_reference" not in repr(execution)

    disabled = catalog.disable(saved.id, expected_revision=verified.revision)
    assert disabled.status is ModelConfigStatus.DISABLED
    assert disabled.revision == 3
    assert disabled.updated_at == NOW + timedelta(microseconds=3)
    with pytest.raises(ModelNotVerified):
        catalog.require_verified(saved.id)
    with pytest.raises(ModelCatalogConflict):
        catalog.mark_test_result(
            saved.id,
            expected_status=ModelConfigStatus.DISABLED,
            expected_revision=disabled.revision,
            succeeded=True,
        )


def test_revision_cas_blocks_two_catalogs_stale_writes_and_status_aba(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'cas.db'}"
    migrate(url)
    owner = AnalysisModelCatalog(create_engine_for_url(url), clock=lambda: NOW)
    peer = AnalysisModelCatalog(create_engine_for_url(url), clock=lambda: NOW)
    try:
        saved = owner.create(display_name="Local", public_config=_config())
        stale = peer.get(saved.id)
        failed = owner.mark_test_result(
            saved.id,
            expected_status=ModelConfigStatus.UNVERIFIED,
            expected_revision=saved.revision,
            succeeded=False,
            error_code="connection_failed",
        )
        verified = owner.mark_test_result(
            saved.id,
            expected_status=ModelConfigStatus.FAILED,
            expected_revision=failed.revision,
            succeeded=True,
        )
        failed_again = owner.mark_test_result(
            saved.id,
            expected_status=ModelConfigStatus.VERIFIED,
            expected_revision=verified.revision,
            succeeded=False,
            error_code="connection_failed",
        )

        with pytest.raises(ModelCatalogConflict):
            peer.update_display_name(
                saved.id, "Stale rename", expected_revision=stale.revision
            )
        with pytest.raises(ModelCatalogConflict):
            peer.mark_test_result(
                saved.id,
                expected_status=ModelConfigStatus.FAILED,
                expected_revision=failed.revision,
                succeeded=True,
            )
        with pytest.raises(ModelCatalogConflict):
            peer.disable(saved.id, expected_revision=verified.revision)
        assert failed_again.revision == 3
    finally:
        peer.close()
        owner.close()


def test_fixed_and_regressing_clocks_allow_same_status_retests_with_monotonic_time(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'regressing-clock.db'}"
    migrate(url)
    samples = iter(
        (
            NOW,
            NOW - timedelta(days=1),
            NOW - timedelta(days=2),
            NOW - timedelta(days=3),
            NOW - timedelta(days=4),
        )
    )
    catalog = AnalysisModelCatalog(
        create_engine_for_url(url), clock=lambda: next(samples)
    )
    try:
        saved = catalog.create(display_name="Local", public_config=_config())
        verified = catalog.mark_test_result(
            saved.id,
            expected_status=ModelConfigStatus.UNVERIFIED,
            expected_revision=saved.revision,
            succeeded=True,
        )
        verified_again = catalog.mark_test_result(
            saved.id,
            expected_status=ModelConfigStatus.VERIFIED,
            expected_revision=verified.revision,
            succeeded=True,
        )
        failed = catalog.mark_test_result(
            saved.id,
            expected_status=ModelConfigStatus.VERIFIED,
            expected_revision=verified_again.revision,
            succeeded=False,
            error_code="connection_failed",
        )
        failed_again = catalog.mark_test_result(
            saved.id,
            expected_status=ModelConfigStatus.FAILED,
            expected_revision=failed.revision,
            succeeded=False,
            error_code="connection_failed",
        )

        assert (
            saved.updated_at
            < verified.updated_at
            < verified_again.updated_at
            < failed.updated_at
            < failed_again.updated_at
        )
        assert verified_again.verified_at == verified_again.last_tested_at
        assert verified_again.last_tested_at > verified.last_tested_at
        assert failed_again.last_tested_at > failed.last_tested_at
    finally:
        catalog.close()


def test_display_test_and_disable_all_advance_updated_at_monotonically(
    catalog: AnalysisModelCatalog,
) -> None:
    saved = catalog.create(display_name="Local", public_config=_config())
    renamed = catalog.update_display_name(
        saved.id, "Renamed", expected_revision=saved.revision
    )
    tested = catalog.mark_test_result(
        saved.id,
        expected_status=ModelConfigStatus.UNVERIFIED,
        expected_revision=renamed.revision,
        succeeded=True,
    )
    disabled = catalog.disable(saved.id, expected_revision=tested.revision)

    assert (
        saved.updated_at < renamed.updated_at < tested.updated_at < disabled.updated_at
    )


def test_disabled_is_terminal_for_catalog_mutations(
    catalog: AnalysisModelCatalog,
) -> None:
    saved = catalog.create(display_name="Local", public_config=_config())
    disabled = catalog.disable(saved.id, expected_revision=saved.revision)

    with pytest.raises(ModelCatalogConflict):
        catalog.disable(saved.id, expected_revision=disabled.revision)
    with pytest.raises(ModelCatalogConflict):
        catalog.update_display_name(
            saved.id, "No rename", expected_revision=disabled.revision
        )


@pytest.mark.parametrize("codepoint", [*range(32), 127])
def test_display_name_rejects_every_c0_and_del_character(
    catalog: AnalysisModelCatalog, codepoint: int
) -> None:
    with pytest.raises(ValueError):
        catalog.create(
            display_name=f"bad{chr(codepoint)}name",
            public_config=_config(),
        )


def test_second_successor_for_the_same_parent_conflicts(
    catalog: AnalysisModelCatalog,
) -> None:
    first = catalog.create(display_name="Local", public_config=_config())
    catalog.create_successor(
        first.id,
        display_name="First successor",
        public_config=_config(model="successor-1"),
    )

    with pytest.raises(ModelCatalogConflict):
        catalog.create_successor(
            first.id,
            display_name="Second successor",
            public_config=_config(model="successor-2"),
        )


@pytest.mark.parametrize("corruption", ["digest", "extra_json", "revision", "audit"])
def test_get_fails_closed_for_stored_digest_json_revision_and_audit_corruption(
    catalog: AnalysisModelCatalog, corruption: str
) -> None:
    saved = catalog.create(display_name="Local", public_config=_config())
    with catalog.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_analysis_model_config_immutable_update"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER trg_analysis_model_config_mutation_guard"
        )
        if corruption == "digest":
            payload = json.loads(
                connection.exec_driver_sql(
                    "SELECT public_config_json FROM analysis_model_config WHERE id=?",
                    (saved.id,),
                ).scalar_one()
            )
            payload["temperature"] = 0.2
            connection.exec_driver_sql(
                "UPDATE analysis_model_config SET public_config_json=? WHERE id=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), saved.id),
            )
        elif corruption == "extra_json":
            payload = json.loads(
                connection.exec_driver_sql(
                    "SELECT public_config_json FROM analysis_model_config WHERE id=?",
                    (saved.id,),
                ).scalar_one()
            )
            payload["unexpected"] = "persisted"
            connection.exec_driver_sql(
                "UPDATE analysis_model_config SET public_config_json=? WHERE id=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), saved.id),
            )
        else:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            if corruption == "revision":
                connection.exec_driver_sql(
                    "UPDATE analysis_model_config SET revision=-1 WHERE id=?",
                    (saved.id,),
                )
            else:
                connection.exec_driver_sql(
                    "UPDATE analysis_model_config SET updated_at='2000-01-01T00:00:00Z' "
                    "WHERE id=?",
                    (saved.id,),
                )

    with pytest.raises(ModelCatalogCorruption):
        catalog.get(saved.id)


def test_get_list_duplicate_content_not_found_identity_and_closed_boundaries(
    catalog: AnalysisModelCatalog,
) -> None:
    beta = catalog.create(display_name="Beta", public_config=_config(model="b"))
    alpha = catalog.create(display_name="Alpha", public_config=_config(model="a"))

    assert catalog.get(alpha.id) == alpha
    assert catalog.list_page(limit=100, include_disabled=True).items == tuple(
        sorted((alpha, beta), key=lambda item: item.id)
    )
    assert not hasattr(catalog, "list")
    assert catalog.database_identity
    with pytest.raises(ModelCatalogConflict):
        catalog.create(display_name="Duplicate", public_config=_config(model="a"))
    with pytest.raises(ModelNotFound):
        catalog.get("sha256:" + "f" * 64)
    catalog.close()
    with pytest.raises(ModelCatalogClosed):
        catalog.list_page(limit=100)


def test_atomic_database_file_replacement_permanently_closes_catalog(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.db"
    replacement = tmp_path / "replacement.db"
    original_inode = tmp_path / "original-inode.db"
    migrate(f"sqlite:///{database}")
    migrate(f"sqlite:///{replacement}")
    catalog = AnalysisModelCatalog(
        create_engine_for_url(f"sqlite:///{database}"), clock=lambda: NOW
    )
    saved = catalog.create(display_name="Local", public_config=_config())
    catalog.engine.dispose()
    os.replace(database, original_inode)
    os.replace(replacement, database)
    try:
        with pytest.raises(ModelCatalogClosed, match="identity"):
            catalog.get(saved.id)
        with pytest.raises(ModelCatalogClosed, match="closed"):
            catalog.list_page(limit=100)
    finally:
        catalog.close()


def test_mutations_return_their_own_transaction_snapshot_without_post_commit_get(
    catalog: AnalysisModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        catalog,
        "get",
        lambda _config_id: (_ for _ in ()).throw(
            AssertionError("post-commit get must not be used")
        ),
    )

    saved = catalog.create(display_name="Local", public_config=_config())
    renamed = catalog.update_display_name(
        saved.id, "Renamed", expected_revision=saved.revision
    )
    tested = catalog.mark_test_result(
        saved.id,
        expected_status=ModelConfigStatus.UNVERIFIED,
        expected_revision=renamed.revision,
        succeeded=True,
    )
    disabled = catalog.disable(saved.id, expected_revision=tested.revision)

    assert (saved.revision, renamed.revision, tested.revision, disabled.revision) == (
        0,
        1,
        2,
        3,
    )


def test_transaction_snapshot_cannot_be_replaced_by_peer_post_commit_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'returning-race.db'}"
    migrate(url)
    owner = AnalysisModelCatalog(create_engine_for_url(url), clock=lambda: NOW)
    peer = AnalysisModelCatalog(create_engine_for_url(url), clock=lambda: NOW)
    try:
        saved = owner.create(display_name="Local", public_config=_config())
        real_begin = owner._begin

        @contextmanager
        def racing_begin():
            with real_begin() as connection:
                yield connection
            current = peer.get(saved.id)
            peer.update_display_name(
                saved.id, "Peer revision", expected_revision=current.revision
            )

        monkeypatch.setattr(owner, "_begin", racing_begin)
        returned = owner.update_display_name(
            saved.id, "Owner revision", expected_revision=saved.revision
        )

        assert returned.revision == 1
        assert returned.display_name == "Owner revision"
        assert peer.get(saved.id).revision == 2
    finally:
        peer.close()
        owner.close()


def test_list_page_is_stable_bounded_complete_and_filters_disabled(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'pages.db'}"
    migrate(url)
    catalog = AnalysisModelCatalog(create_engine_for_url(url), clock=lambda: NOW)
    try:
        created = tuple(
            catalog.create(
                display_name=f"Model {index % 17:02d}",
                public_config=_config(model=f"model-{index:03d}"),
            )
            for index in range(137)
        )
        disabled_ids = {
            item.id
            for item in created[::13]
            if catalog.disable(item.id, expected_revision=item.revision)
        }

        seen: list[str] = []
        after: ModelConfigListKey | None = None
        while True:
            page = catalog.list_page(limit=19, after=after, include_disabled=False)
            assert 0 < len(page.items) <= 19
            seen.extend(item.id for item in page.items)
            if page.next_key is None:
                break
            after = page.next_key

        expected = [
            item.id
            for item in sorted(created, key=lambda item: item.id)
            if item.id not in disabled_ids
        ]
        assert seen == expected
        assert len(seen) == len(set(seen))
        with_disabled = catalog.list_page(limit=100, include_disabled=True)
        assert len(with_disabled.items) == 100
    finally:
        catalog.close()


def test_id_keyset_pagination_is_immune_to_renaming_an_unread_item(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'rename-page.db'}"
    migrate(url)
    owner = AnalysisModelCatalog(create_engine_for_url(url), clock=lambda: NOW)
    peer = AnalysisModelCatalog(create_engine_for_url(url), clock=lambda: NOW)
    try:
        created = tuple(
            owner.create(
                display_name=f"Name {index:03d}",
                public_config=_config(model=f"rename-model-{index:03d}"),
            )
            for index in range(41)
        )
        expected_ids = sorted(item.id for item in created)
        first = owner.list_page(limit=7, include_disabled=True)
        unread_id = expected_ids[-1]
        unread = peer.get(unread_id)
        peer.update_display_name(
            unread.id,
            "AAA renamed across display order",
            expected_revision=unread.revision,
        )

        seen = [item.id for item in first.items]
        after = first.next_key
        while after is not None:
            page = owner.list_page(limit=7, after=after, include_disabled=True)
            seen.extend(item.id for item in page.items)
            after = page.next_key

        assert seen == expected_ids
        assert len(seen) == len(set(seen))
    finally:
        peer.close()
        owner.close()


def test_bad_database_timestamp_is_wrapped_as_catalog_corruption(
    catalog: AnalysisModelCatalog,
) -> None:
    saved = catalog.create(display_name="Local", public_config=_config())
    with catalog.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(
            "DROP TRIGGER trg_analysis_model_config_immutable_update"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER trg_analysis_model_config_mutation_guard"
        )
        connection.exec_driver_sql(
            "UPDATE analysis_model_config SET created_at='0000-01-01 00:00:00.000000', "
            "updated_at='0000-01-01 00:00:00.000000' WHERE id=?",
            (saved.id,),
        )

    with pytest.raises(ModelCatalogCorruption):
        catalog.get(saved.id)


@pytest.mark.parametrize("limit", [0, 101, -1, True])
def test_list_page_rejects_invalid_limits(
    catalog: AnalysisModelCatalog, limit: object
) -> None:
    with pytest.raises(ValueError):
        catalog.list_page(limit=limit)  # type: ignore[arg-type]
