from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from stock_desk.formula.repository import (
    FormulaConflict,
    FormulaNotFound,
    FormulaRepository,
    FormulaRepositoryError,
    FormulaValidationError,
)
from stock_desk.storage.database import create_engine_for_url, migrate


MACD = "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[FormulaRepository]:
    url = f"sqlite:///{tmp_path / 'formula.db'}"
    migrate(url)
    repository = FormulaRepository(create_engine_for_url(url))
    yield repository
    repository.close()


def _save(
    repository: FormulaRepository,
    formula_id: str,
    source: str,
    parameter_schema: dict[str, object] | None = None,
):
    revision = repository.get_draft(formula_id).revision
    return repository.save(
        formula_id,
        source,
        parameter_schema or {},
        expected_revision=revision,
    )


def test_save_creates_new_version_without_mutating_old(
    repository: FormulaRepository,
) -> None:
    first = repository.create("MACD", "trading", MACD, {}, placement="subchart")
    changed = MACD.replace("12", "10", 1)

    second = _save(repository, first.formula_id, changed)

    assert (first.version, second.version) == (1, 2)
    assert repository.get_version(first.id).source == MACD
    assert repository.get_version(second.id).source == changed
    assert repository.get_draft(first.formula_id).executable_version_id == second.id


def test_invalid_draft_is_not_executable(repository: FormulaRepository) -> None:
    draft = repository.save_draft("Broken", "X:UNKNOWN(CLOSE);")

    assert draft.executable_version_id is None
    assert draft.validation_result[0]["code"] == "unsupported_function"
    assert repository.list_versions(draft.formula_id) == ()
    with pytest.raises(FormulaValidationError):
        repository.save(
            draft.formula_id,
            draft.source,
            {},
            expected_revision=draft.revision,
        )


def test_valid_draft_edit_is_not_executable_until_published(
    repository: FormulaRepository,
) -> None:
    published = repository.create("Draft", "indicator", "X:C;", {})
    draft = repository.get_draft(published.formula_id)

    edited = repository.update_draft(
        published.formula_id, "X:O;", {}, expected_revision=draft.revision
    )

    assert edited.revision == draft.revision + 1
    assert edited.validation_result == ()
    assert edited.executable_version_id is None
    assert repository.get_version(published.id) == published


def test_stale_draft_revision_is_rejected(repository: FormulaRepository) -> None:
    draft = repository.save_draft("CAS", "X:C;")
    repository.update_draft(
        draft.formula_id, "X:O;", {}, expected_revision=draft.revision
    )

    with pytest.raises(FormulaConflict, match="draft changed concurrently"):
        repository.update_draft(
            draft.formula_id, "X:H;", {}, expected_revision=draft.revision
        )


def test_publish_rejects_a_stale_draft_revision(
    repository: FormulaRepository,
) -> None:
    published = repository.create("Publish CAS", "indicator", "X:C;", {})
    original = repository.get_draft(published.formula_id)
    current = repository.update_draft(
        published.formula_id, "X:O;", {}, expected_revision=original.revision
    )

    with pytest.raises(FormulaConflict, match="draft changed concurrently"):
        repository.save(
            published.formula_id,
            "X:H;",
            {},
            expected_revision=original.revision,
        )

    assert repository.get_draft(published.formula_id) == current
    assert repository.get_formula(published.formula_id).latest_version == 1


def test_trading_draft_reports_missing_buy_and_sell_outputs(
    repository: FormulaRepository,
) -> None:
    draft = repository.save_draft(
        "Trading Draft", "X:C;", formula_type="trading", placement="main"
    )

    assert draft.executable_version_id is None
    assert draft.validation_result[0]["code"] == "missing_trading_signals"


def test_invalid_trading_publish_clears_previous_executable_pointer(
    repository: FormulaRepository,
) -> None:
    published = repository.create("Trading", "trading", MACD, {}, placement="main")
    revision = repository.get_draft(published.formula_id).revision

    with pytest.raises(FormulaValidationError, match="cannot be published"):
        repository.save(published.formula_id, "X:C;", {}, expected_revision=revision)

    draft = repository.get_draft(published.formula_id)
    assert draft.executable_version_id is None
    assert draft.validation_result[0]["code"] == "missing_trading_signals"
    assert repository.get_version(published.id) == published


def test_invalid_edit_clears_pointer_without_changing_published_version(
    repository: FormulaRepository,
) -> None:
    published = repository.create("Published", "indicator", "X:C;", {})

    with pytest.raises(FormulaValidationError, match="cannot be published"):
        repository.save(
            published.formula_id,
            "X:UNKNOWN(C);",
            {},
            expected_revision=repository.get_draft(published.formula_id).revision,
        )

    draft = repository.get_draft(published.formula_id)
    assert draft.executable_version_id is None
    assert draft.validation_result[0]["code"] == "unsupported_function"
    assert repository.get_version(published.id) == published
    assert repository.list_versions(published.formula_id) == (published,)


def test_copy_creates_independent_formula_identity(
    repository: FormulaRepository,
) -> None:
    source = repository.create("MACD", "trading", MACD, {}, placement="main")

    copied = repository.copy(source.formula_id, "MACD Copy")
    updated = _save(repository, copied.formula_id, MACD.replace("26", "30", 1))

    assert copied.formula_id != source.formula_id
    assert copied.version == 1 and updated.version == 2
    assert repository.list_versions(source.formula_id) == (source,)
    assert repository.get_formula(copied.formula_id).name == "MACD Copy"


def test_copy_can_pin_an_exact_historical_version(
    repository: FormulaRepository,
) -> None:
    first = repository.create("Source", "indicator", "X:C;", {})
    _save(repository, first.formula_id, "X:O;")

    copied = repository.copy(
        first.formula_id, "Historical Copy", source_version_id=first.id
    )

    assert copied.name == "Historical Copy"
    assert copied.source == first.source
    assert copied.copied_from_version_id == first.id


def test_copy_rejects_a_version_owned_by_another_formula(
    repository: FormulaRepository,
) -> None:
    first = repository.create("First Owner", "indicator", "X:C;", {})
    second = repository.create("Second Owner", "indicator", "X:O;", {})

    with pytest.raises(FormulaNotFound, match="formula version does not exist"):
        repository.copy(
            first.formula_id,
            "Wrong Owner",
            source_version_id=second.id,
        )


def test_concurrent_saves_allocate_unique_monotonic_versions(
    repository: FormulaRepository,
) -> None:
    first = repository.create("Concurrent", "indicator", "X:C;", {})

    def save(index: int) -> int:
        for _attempt in range(16):
            draft = repository.get_draft(first.formula_id)
            try:
                return repository.save(
                    first.formula_id,
                    f"X:C+{index};",
                    {},
                    expected_revision=draft.revision,
                ).version
            except FormulaConflict:
                continue
        raise AssertionError("save did not converge")

    with ThreadPoolExecutor(max_workers=8) as executor:
        versions = tuple(executor.map(save, range(1, 9)))

    assert sorted(versions) == list(range(2, 10))
    assert tuple(
        item.version for item in repository.list_versions(first.formula_id)
    ) == tuple(range(1, 10))


def test_independent_engines_allocate_unique_monotonic_versions(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'independent-engines.db'}"
    migrate(url)
    owner = FormulaRepository(create_engine_for_url(url))
    try:
        first = owner.create("Independent", "indicator", "X:C;", {})

        def save(index: int) -> int:
            worker = FormulaRepository(create_engine_for_url(url))
            try:
                for _attempt in range(16):
                    draft = worker.get_draft(first.formula_id)
                    try:
                        return worker.save(
                            first.formula_id,
                            f"X:C+{index};",
                            {},
                            expected_revision=draft.revision,
                        ).version
                    except FormulaConflict:
                        continue
                raise AssertionError("save did not converge")
            finally:
                worker.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            versions = tuple(executor.map(save, range(1, 9)))

        assert sorted(versions) == list(range(2, 10))
        assert tuple(
            item.version for item in owner.list_versions(first.formula_id)
        ) == (*range(1, 10),)
    finally:
        owner.close()


def test_failed_version_insert_rolls_back_counter_without_gap(
    repository: FormulaRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = repository.create("Rollback", "indicator", "X:C;", {})
    original = repository._version_values

    def invalid_values(*args: object, **kwargs: object) -> dict[str, object]:
        values = original(*args, **kwargs)  # type: ignore[arg-type]
        values["source"] = ""
        return values

    monkeypatch.setattr(repository, "_version_values", invalid_values)
    with pytest.raises(IntegrityError):
        _save(repository, first.formula_id, "X:O;")
    monkeypatch.setattr(repository, "_version_values", original)

    second = _save(repository, first.formula_id, "X:H;")
    assert second.version == 2
    assert repository.get_formula(first.formula_id).latest_version == 2


def test_published_versions_are_database_immutable(
    repository: FormulaRepository,
) -> None:
    version = repository.create("Immutable", "indicator", "X:C;", {})
    with repository.engine.begin() as connection:
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(
                text("UPDATE formula_version SET source = 'X:O;' WHERE id = :id"),
                {"id": version.id},
            )


def test_insert_or_replace_cannot_overwrite_published_identity(
    repository: FormulaRepository,
) -> None:
    version = repository.create("Replace", "indicator", "X:C;", {})

    with repository.engine.begin() as connection:
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO formula_version "
                    "SELECT * FROM formula_version WHERE id = :id"
                ),
                {"id": version.id},
            )


def test_insert_or_replace_cannot_overwrite_published_version_number(
    repository: FormulaRepository,
) -> None:
    version = repository.create("Replace Number", "indicator", "X:C;", {})

    with repository.engine.begin() as connection:
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO formula_version "
                    "(id,formula_id,version,name,formula_type,placement,source,"
                    "parameter_schema_json,compatibility_version,engine_version,"
                    "checksum,validation_result_json,copied_from_version_id,created_at) "
                    "SELECT :new_id,formula_id,version,name,formula_type,placement,source,"
                    "parameter_schema_json,compatibility_version,engine_version,"
                    "checksum,validation_result_json,copied_from_version_id,created_at "
                    "FROM formula_version WHERE id = :id"
                ),
                {"id": version.id, "new_id": "replacement-version"},
            )
    with repository.engine.begin() as connection:
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(
                text("DELETE FROM formula_version WHERE id = :id"),
                {"id": version.id},
            )


@pytest.mark.parametrize(
    ("formula_type", "placement"),
    [("selector", "main"), ("indicator", "floating")],
)
def test_database_rejects_unsupported_type_and_placement(
    repository: FormulaRepository, formula_type: str, placement: str
) -> None:
    with repository.engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO formula "
                    "(id,name,formula_type,placement,latest_version,created_at,updated_at) "
                    "VALUES ('bad','Bad',:type,:placement,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {"type": formula_type, "placement": placement},
            )


def test_version_snapshot_contains_reproducibility_fields(
    repository: FormulaRepository,
) -> None:
    version = repository.create("Fields", "indicator", "X:C;", {})
    draft = repository.get_draft(version.formula_id)
    assert version.name == "Fields"
    assert version.parameter_schema == {}
    assert version.compatibility_version == "tdx-v1"
    assert version.engine_version == "formula-engine-v1"
    assert version.checksum.startswith("sha256:")
    assert version.validation_result[0]["code"] == "validated"
    assert version.validation_result[0]["source_checksum"] == version.checksum
    assert draft.source_checksum == version.checksum
    assert version.created_at.tzinfo is not None


def test_domain_json_snapshots_are_deeply_immutable(
    repository: FormulaRepository,
) -> None:
    version = repository.create(
        "Frozen", "indicator", "X:C+N;", {"N": {"kind": "integer", "default": 1}}
    )

    with pytest.raises(TypeError):
        version.parameter_schema["N"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        version.parameter_schema["N"]["default"] = 2  # type: ignore[index]


def test_database_rejects_cross_formula_executable_pointer(
    repository: FormulaRepository,
) -> None:
    first = repository.create("First", "indicator", "X:C;", {})
    second = repository.create("Second", "indicator", "X:O;", {})

    with repository.engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE formula_draft SET executable_version_id = :version_id "
                    "WHERE formula_id = :formula_id"
                ),
                {"formula_id": first.formula_id, "version_id": second.id},
            )


def test_database_source_limit_counts_utf8_bytes(
    repository: FormulaRepository,
) -> None:
    draft = repository.save_draft("Byte Limit", "X:C;")

    with repository.engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE formula_draft SET source = :source, source_checksum = :checksum "
                    "WHERE formula_id = :formula_id"
                ),
                {
                    "formula_id": draft.formula_id,
                    "source": "股" * 22_000,
                    "checksum": "sha256:" + "a" * 64,
                },
            )


@pytest.mark.parametrize(
    ("assignment", "parameters"),
    [
        (
            "checksum = :value",
            {"value": "sha256:" + "f" * 64},
        ),
        ("validation_result_json = :value", {"value": "[]"}),
        ("parameter_schema_json = :value", {"value": "{"}),
    ],
)
def test_corrupt_published_rows_are_rejected_with_stable_error(
    repository: FormulaRepository,
    assignment: str,
    parameters: dict[str, str],
) -> None:
    version = repository.create("Corrupt", "indicator", "X:C;", {})
    with repository.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_formula_version_immutable_update"))
        connection.execute(
            text(f"UPDATE formula_version SET {assignment} WHERE id = :id"),
            {**parameters, "id": version.id},
        )

    with pytest.raises(
        FormulaRepositoryError, match=r"^formula catalog data is invalid$"
    ):
        repository.get_version(version.id)


def test_corrupt_draft_checksum_is_rejected_with_stable_error(
    repository: FormulaRepository,
) -> None:
    version = repository.create("Draft Corrupt", "indicator", "X:C;", {})
    with repository.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE formula_draft SET source_checksum = :checksum "
                "WHERE formula_id = :formula_id"
            ),
            {
                "checksum": "sha256:" + "f" * 64,
                "formula_id": version.formula_id,
            },
        )

    with pytest.raises(
        FormulaRepositoryError, match=r"^formula catalog data is invalid$"
    ):
        repository.get_draft(version.formula_id)


def test_corrupt_version_counter_is_rejected_on_every_read_path(
    repository: FormulaRepository,
) -> None:
    version = repository.create("Counter Corrupt", "indicator", "X:C;", {})
    with repository.engine.begin() as connection:
        connection.execute(
            text("UPDATE formula SET latest_version = 7 WHERE id = :formula_id"),
            {"formula_id": version.formula_id},
        )

    for read in (
        lambda: repository.get_formula(version.formula_id),
        lambda: repository.get_version(version.id),
        lambda: repository.list_versions(version.formula_id),
    ):
        with pytest.raises(
            FormulaRepositoryError, match=r"^formula catalog data is invalid$"
        ):
            read()


def test_parameter_schema_limits_are_stable_validation_errors(
    repository: FormulaRepository,
) -> None:
    deeply_nested: dict[str, object] = {}
    cursor = deeply_nested
    for _ in range(2_000):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child

    invalid_schemas = (
        {"N": {"kind": "integer", "default": 1, "label": "\ud800"}},
        {"N": {"kind": "integer", "default": 1, "label": "x" * 70_000}},
        {"N": deeply_nested},
    )
    for schema in invalid_schemas:
        with pytest.raises(FormulaValidationError, match="parameter schema is invalid"):
            repository.create("Bad Schema", "indicator", "X:C;", schema)


@pytest.mark.parametrize(
    "source",
    ["", "X:" + "1" * 64_001 + ";", "X:'\ud800';"],
)
def test_draft_source_boundaries_are_stable_validation_errors(
    repository: FormulaRepository, source: str
) -> None:
    with pytest.raises(FormulaValidationError, match=r"^formula source is invalid$"):
        repository.save_draft("Bad Source", source)

    draft = repository.save_draft("Existing", "X:C;")
    with pytest.raises(FormulaValidationError, match=r"^formula source is invalid$"):
        repository.update_draft(
            draft.formula_id, source, {}, expected_revision=draft.revision
        )


def test_copy_rejects_corrupt_source_version(repository: FormulaRepository) -> None:
    version = repository.create("Copy Corrupt", "indicator", "X:C;", {})
    with repository.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_formula_version_immutable_update"))
        connection.execute(
            text("UPDATE formula_version SET checksum = :checksum WHERE id = :id"),
            {"checksum": "sha256:" + "f" * 64, "id": version.id},
        )

    with pytest.raises(
        FormulaRepositoryError, match=r"^formula catalog data is invalid$"
    ):
        repository.copy(version.formula_id, "Rejected Copy")


def test_save_rejects_corrupt_formula_identity(repository: FormulaRepository) -> None:
    version = repository.create("Header", "indicator", "X:C;", {})
    with repository.engine.begin() as connection:
        connection.execute(
            text("UPDATE formula SET name = 'Changed' WHERE id = :formula_id"),
            {"formula_id": version.formula_id},
        )

    with pytest.raises(
        FormulaRepositoryError, match=r"^formula catalog data is invalid$"
    ):
        repository.save(
            version.formula_id,
            "X:O;",
            {},
            expected_revision=repository.get_draft(version.formula_id).revision,
        )
