from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Engine, and_, func, insert, or_, select, update
from sqlalchemy.engine import Connection, RowMapping

from stock_desk.formula.compiler import compile_formula, formula_source_checksum
from stock_desk.formula.functions.base import IDENTIFIER_PATTERN
from stock_desk.formula.models import (
    Formula,
    FormulaDraft,
    FormulaDraftRow,
    FormulaPlacement,
    FormulaRow,
    FormulaType,
    FormulaVersion,
    FormulaVersionRow,
)
from stock_desk.formula.signal_series import COMPATIBILITY_VERSION, ENGINE_VERSION
from stock_desk.formula.validator import FormulaValidator
from stock_desk.formula.values import IntegerScalar, NumberScalar, ScalarValue


class FormulaRepositoryError(Exception):
    """Base class for stable formula catalog persistence failures."""


class FormulaNotFound(FormulaRepositoryError):
    pass


class FormulaConflict(FormulaRepositoryError):
    pass


class FormulaValidationError(FormulaRepositoryError, ValueError):
    pass


class FormulaCursorError(FormulaValidationError):
    pass


_MAX_PARAMETER_SCHEMA_BYTES = 64_000
_MAX_JSON_DEPTH = 16
_MAX_JSON_NODES = 1_024


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    raise FormulaRepositoryError("formula timestamp is invalid")


def _normalize_json(
    value: object,
    *,
    depth: int,
    remaining_nodes: list[int],
    active_containers: set[int],
) -> Any:
    if depth > _MAX_JSON_DEPTH or remaining_nodes[0] <= 0:
        raise ValueError
    remaining_nodes[0] -= 1
    if value is None or type(value) in {bool, int}:
        if type(value) is int and abs(value) > 2**53:
            raise ValueError
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError
        return value
    if type(value) is str:
        value.encode("utf-8")
        return value
    if not isinstance(value, (Mapping, list, tuple)):
        raise TypeError
    identity = id(value)
    if identity in active_containers:
        raise ValueError
    active_containers.add(identity)
    try:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError
                key.encode("utf-8")
                result[key] = _normalize_json(
                    item,
                    depth=depth + 1,
                    remaining_nodes=remaining_nodes,
                    active_containers=active_containers,
                )
            return dict(sorted(result.items()))
        return [
            _normalize_json(
                item,
                depth=depth + 1,
                remaining_nodes=remaining_nodes,
                active_containers=active_containers,
            )
            for item in value
        ]
    finally:
        active_containers.remove(identity)


def _canonical_json(value: object, label: str) -> dict[str, Any]:
    try:
        payload = _normalize_json(
            value,
            depth=0,
            remaining_nodes=[_MAX_JSON_NODES],
            active_containers=set(),
        )
        if not isinstance(payload, dict):
            raise TypeError
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_PARAMETER_SCHEMA_BYTES:
            raise ValueError
    except (RecursionError, RuntimeError, TypeError, UnicodeError, ValueError):
        raise FormulaValidationError(f"{label} is invalid") from None
    return cast(dict[str, Any], payload)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _source_checksum(source: object) -> str:
    if type(source) is not str or source == "":
        raise FormulaValidationError("formula source is invalid")
    try:
        return formula_source_checksum(source)
    except (TypeError, ValueError):
        raise FormulaValidationError("formula source is invalid") from None


def _parameter_schema_checksum(schema: Mapping[str, Any]) -> str:
    payload = json.dumps(
        schema,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _published_report(
    source_checksum: str, schema: Mapping[str, Any]
) -> list[dict[str, str]]:
    return [
        {
            "code": "validated",
            "compatibility_version": COMPATIBILITY_VERSION,
            "engine_version": ENGINE_VERSION,
            "parameter_schema_checksum": _parameter_schema_checksum(schema),
            "source_checksum": source_checksum,
        }
    ]


def _parameter_values(schema: Mapping[str, Any]) -> dict[str, ScalarValue]:
    values: dict[str, ScalarValue] = {}
    for name, raw in schema.items():
        if IDENTIFIER_PATTERN.fullmatch(name) is None or not isinstance(raw, Mapping):
            raise FormulaValidationError("parameter schema is invalid")
        kind = raw.get("kind")
        default = raw.get("default")
        if kind == "integer" and type(default) is int:
            values[name] = IntegerScalar(default)
        elif kind == "number" and type(default) is float and math.isfinite(default):
            values[name] = NumberScalar(default)
        else:
            raise FormulaValidationError("parameter schema is invalid")
    return dict(sorted(values.items()))


def normalize_parameter_schema(
    value: object,
) -> tuple[dict[str, Any], dict[str, ScalarValue]]:
    """Return the canonical schema and its validated default parameter values."""
    schema = _canonical_json(value, "parameter schema")
    return schema, _parameter_values(schema)


def _diagnostic_data(
    source: str, parameters: Mapping[str, ScalarValue]
) -> tuple[dict[str, Any], ...]:
    diagnostics = FormulaValidator().validate(source, parameters=parameters)
    return tuple(
        {
            "blocks_backtest": item.blocks_backtest,
            "blocks_preview": item.blocks_preview,
            "blocks_save": item.blocks_save,
            "code": item.code,
            "explanation": item.explanation,
            "function": item.function,
            "span": {
                "column": item.span.column,
                "end_column": item.span.end_column,
                "end_line": item.span.end_line,
                "line": item.span.line,
            },
        }
        for item in diagnostics
    )


def _formula_diagnostics(
    source: str,
    parameters: Mapping[str, ScalarValue],
    formula_type: FormulaType,
) -> tuple[dict[str, Any], ...]:
    diagnostics = _diagnostic_data(source, parameters)
    if diagnostics or formula_type != "trading":
        return diagnostics
    compiled = compile_formula(source, parameters=parameters)
    if compiled.signal_outputs == ("BUY", "SELL"):
        return ()
    return (
        {
            "blocks_backtest": True,
            "blocks_preview": True,
            "blocks_save": True,
            "code": "missing_trading_signals",
            "explanation": "trading formula requires BUY and SELL outputs",
            "function": None,
            "span": {"column": 1, "end_column": 1, "end_line": 1, "line": 1},
        },
    )


def _validate_identity(
    name: str, formula_type: str, placement: str
) -> tuple[FormulaType, FormulaPlacement]:
    if type(name) is not str or not 0 < len(name) <= 64 or name.strip() != name:
        raise FormulaValidationError("formula name is invalid")
    if formula_type not in {"indicator", "trading"}:
        raise FormulaValidationError("formula type is invalid")
    if placement not in {"main", "subchart"}:
        raise FormulaValidationError("formula placement is invalid")
    return cast(FormulaType, formula_type), cast(FormulaPlacement, placement)


def _catalog_corruption() -> FormulaRepositoryError:
    return FormulaRepositoryError("formula catalog data is invalid")


def _row_schema(value: object) -> dict[str, Any]:
    try:
        schema, _parameters = normalize_parameter_schema(value)
        return schema
    except FormulaValidationError:
        raise _catalog_corruption() from None


def _row_json_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 64:
        raise _catalog_corruption()
    result: list[dict[str, Any]] = []
    for item in value:
        try:
            result.append(_canonical_json(item, "validation result"))
        except FormulaValidationError:
            raise _catalog_corruption() from None
    return result


def _formula(row: RowMapping) -> Formula:
    try:
        formula_id = row["id"]
        name = row["name"]
        formula_type = row["formula_type"]
        placement = row["placement"]
        latest_version = row["latest_version"]
        if (
            type(formula_id) is not str
            or not formula_id
            or type(latest_version) is not int
            or latest_version < 0
        ):
            raise ValueError
        typed_type, typed_placement = _validate_identity(name, formula_type, placement)
        return Formula(
            id=formula_id,
            name=name,
            formula_type=typed_type,
            placement=typed_placement,
            latest_version=latest_version,
            created_at=_utc(row["created_at"]),
            updated_at=_utc(row["updated_at"]),
        )
    except (FormulaRepositoryError, TypeError, ValueError):
        raise _catalog_corruption() from None


def _draft(row: RowMapping, owner: Formula) -> FormulaDraft:
    try:
        formula_id = row["formula_id"]
        revision = row["revision"]
        source = row["source"]
        source_checksum = row["source_checksum"]
        executable_version_id = row["executable_version_id"]
        if (
            formula_id != owner.id
            or type(revision) is not int
            or revision < 1
            or (
                executable_version_id is not None
                and type(executable_version_id) is not str
            )
        ):
            raise ValueError
        actual_checksum = _source_checksum(source)
        if source_checksum != actual_checksum:
            raise ValueError
        schema = _row_schema(row["parameter_schema_json"])
        validation = _row_json_list(row["validation_result_json"])
        if executable_version_id is None:
            expected_diagnostics = list(
                _formula_diagnostics(
                    source, _parameter_values(schema), owner.formula_type
                )
            )
            if validation != expected_diagnostics:
                raise ValueError
        return FormulaDraft(
            formula_id=formula_id,
            revision=revision,
            source=source,
            source_checksum=actual_checksum,
            parameter_schema=_freeze_json(schema),
            validation_result=tuple(_freeze_json(item) for item in validation),
            executable_version_id=executable_version_id,
            updated_at=_utc(row["updated_at"]),
        )
    except (FormulaRepositoryError, TypeError, ValueError):
        raise _catalog_corruption() from None


def _version(row: RowMapping, owner: Formula) -> FormulaVersion:
    try:
        version_id = row["id"]
        formula_id = row["formula_id"]
        version = row["version"]
        name = row["name"]
        formula_type = row["formula_type"]
        placement = row["placement"]
        source = row["source"]
        compatibility_version = row["compatibility_version"]
        engine_version = row["engine_version"]
        checksum = row["checksum"]
        copied_from = row["copied_from_version_id"]
        if (
            type(version_id) is not str
            or not version_id
            or formula_id != owner.id
            or type(version) is not int
            or not 1 <= version <= owner.latest_version
            or name != owner.name
            or formula_type != owner.formula_type
            or placement != owner.placement
            or type(compatibility_version) is not str
            or not compatibility_version
            or type(engine_version) is not str
            or not engine_version
            or (copied_from is not None and type(copied_from) is not str)
        ):
            raise ValueError
        actual_checksum = _source_checksum(source)
        if checksum != actual_checksum:
            raise ValueError
        schema = _row_schema(row["parameter_schema_json"])
        if (
            compatibility_version == COMPATIBILITY_VERSION
            and engine_version == ENGINE_VERSION
            and _formula_diagnostics(
                source, _parameter_values(schema), owner.formula_type
            )
        ):
            raise ValueError
        expected_report = [
            {
                "code": "validated",
                "compatibility_version": compatibility_version,
                "engine_version": engine_version,
                "parameter_schema_checksum": _parameter_schema_checksum(schema),
                "source_checksum": actual_checksum,
            }
        ]
        validation = _row_json_list(row["validation_result_json"])
        if validation != expected_report:
            raise ValueError
        return FormulaVersion(
            id=version_id,
            formula_id=formula_id,
            version=version,
            name=name,
            formula_type=owner.formula_type,
            placement=owner.placement,
            source=source,
            parameter_schema=_freeze_json(schema),
            compatibility_version=compatibility_version,
            engine_version=engine_version,
            checksum=actual_checksum,
            validation_result=tuple(_freeze_json(item) for item in validation),
            copied_from_version_id=copied_from,
            created_at=_utc(row["created_at"]),
        )
    except (FormulaRepositoryError, TypeError, ValueError):
        raise _catalog_corruption() from None


class FormulaRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def close(self) -> None:
        self.engine.dispose()

    def _connection(self) -> Connection:
        connection = self.engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        except BaseException:
            connection.close()
            raise
        return connection

    def _read_connection(self) -> Connection:
        connection = self.engine.connect()
        try:
            connection.exec_driver_sql("BEGIN")
        except BaseException:
            connection.close()
            raise
        return connection

    def _load_formula_row(
        self, connection: Connection, formula_id: str
    ) -> tuple[RowMapping, Formula]:
        try:
            row = (
                connection.execute(
                    select(FormulaRow.__table__).where(FormulaRow.id == formula_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise FormulaNotFound("formula does not exist")
            formula = _formula(row)
            count, maximum = connection.execute(
                select(
                    func.count(FormulaVersionRow.id),
                    func.max(FormulaVersionRow.version),
                ).where(FormulaVersionRow.formula_id == formula_id)
            ).one()
            expected_maximum = formula.latest_version or None
            if count != formula.latest_version or maximum != expected_maximum:
                raise _catalog_corruption()
            return row, formula
        except FormulaNotFound:
            raise
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def _formulas_from_rows(
        self, connection: Connection, rows: list[RowMapping]
    ) -> tuple[Formula, ...]:
        try:
            formulas = tuple(_formula(row) for row in rows)
            if not formulas:
                return ()
            formula_ids = tuple(formula.id for formula in formulas)
            summaries = {
                str(formula_id): (int(count), maximum)
                for formula_id, count, maximum in connection.execute(
                    select(
                        FormulaVersionRow.formula_id,
                        func.count(FormulaVersionRow.id),
                        func.max(FormulaVersionRow.version),
                    )
                    .where(FormulaVersionRow.formula_id.in_(formula_ids))
                    .group_by(FormulaVersionRow.formula_id)
                ).all()
            }
            for formula in formulas:
                count, maximum = summaries.get(formula.id, (0, None))
                if count != formula.latest_version or maximum != (
                    formula.latest_version or None
                ):
                    raise _catalog_corruption()
            return formulas
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def _load_version_row(
        self,
        connection: Connection,
        version_id: str,
        *,
        owner: Formula | None = None,
    ) -> tuple[RowMapping, FormulaVersion]:
        try:
            row = (
                connection.execute(
                    select(FormulaVersionRow.__table__).where(
                        FormulaVersionRow.id == version_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise FormulaNotFound("formula version does not exist")
            resolved_owner = owner
            if resolved_owner is None:
                _, resolved_owner = self._load_formula_row(
                    connection, str(row["formula_id"])
                )
            return row, _version(row, resolved_owner)
        except FormulaNotFound:
            raise
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def _load_draft_row(
        self,
        connection: Connection,
        formula_id: str,
        *,
        owner: Formula | None = None,
    ) -> tuple[RowMapping, FormulaDraft]:
        try:
            resolved_owner = owner
            if resolved_owner is None:
                _, resolved_owner = self._load_formula_row(connection, formula_id)
            row = (
                connection.execute(
                    select(FormulaDraftRow.__table__).where(
                        FormulaDraftRow.formula_id == formula_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise FormulaNotFound("formula draft does not exist")
            draft = _draft(row, resolved_owner)
            if draft.executable_version_id is not None:
                try:
                    _, version = self._load_version_row(
                        connection,
                        draft.executable_version_id,
                        owner=resolved_owner,
                    )
                except FormulaNotFound:
                    raise _catalog_corruption() from None
                if (
                    version.version != resolved_owner.latest_version
                    or version.source != draft.source
                    or version.checksum != draft.source_checksum
                    or dict(version.parameter_schema) != dict(draft.parameter_schema)
                    or version.validation_result != draft.validation_result
                ):
                    raise _catalog_corruption()
            return row, draft
        except FormulaNotFound:
            raise
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def create(
        self,
        name: str,
        formula_type: str,
        source: str,
        parameter_schema: Mapping[str, Any],
        *,
        placement: str | None = None,
    ) -> FormulaVersion:
        resolved_placement = placement or (
            "main" if formula_type == "trading" else "subchart"
        )
        typed_type, typed_placement = _validate_identity(
            name, formula_type, resolved_placement
        )
        schema, parameters = normalize_parameter_schema(parameter_schema)
        source_checksum = _source_checksum(source)
        diagnostics = _formula_diagnostics(source, parameters, typed_type)
        if diagnostics:
            raise FormulaValidationError("formula cannot be published")
        formula_id = str(uuid4())
        version_id = str(uuid4())
        now = _utc_now()
        connection = self._connection()
        try:
            connection.execute(
                insert(FormulaRow),
                {
                    "id": formula_id,
                    "name": name,
                    "formula_type": typed_type,
                    "placement": typed_placement,
                    "latest_version": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                insert(FormulaVersionRow),
                self._version_values(
                    version_id,
                    formula_id,
                    1,
                    name,
                    typed_type,
                    typed_placement,
                    source,
                    schema,
                    now,
                ),
            )
            connection.execute(
                insert(FormulaDraftRow),
                {
                    "formula_id": formula_id,
                    "revision": 1,
                    "source": source,
                    "source_checksum": source_checksum,
                    "parameter_schema_json": schema,
                    "validation_result_json": _published_report(
                        source_checksum, schema
                    ),
                    "executable_version_id": version_id,
                    "updated_at": now,
                },
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction():
                connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_version(version_id)

    def save_draft(
        self,
        name: str,
        source: str,
        parameter_schema: Mapping[str, Any] | None = None,
        *,
        formula_type: str = "indicator",
        placement: str = "subchart",
    ) -> FormulaDraft:
        typed_type, typed_placement = _validate_identity(name, formula_type, placement)
        schema, parameters = normalize_parameter_schema(parameter_schema or {})
        source_checksum = _source_checksum(source)
        diagnostics = _formula_diagnostics(source, parameters, typed_type)
        formula_id = str(uuid4())
        now = _utc_now()
        connection = self._connection()
        try:
            connection.execute(
                insert(FormulaRow),
                {
                    "id": formula_id,
                    "name": name,
                    "formula_type": typed_type,
                    "placement": typed_placement,
                    "latest_version": 0,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                insert(FormulaDraftRow),
                {
                    "formula_id": formula_id,
                    "revision": 1,
                    "source": source,
                    "source_checksum": source_checksum,
                    "parameter_schema_json": schema,
                    "validation_result_json": list(diagnostics),
                    "executable_version_id": None,
                    "updated_at": now,
                },
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction():
                connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_draft(formula_id)

    def update_draft(
        self,
        formula_id: str,
        source: str,
        parameter_schema: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> FormulaDraft:
        if type(expected_revision) is not int or expected_revision < 1:
            raise FormulaValidationError("draft revision is invalid")
        schema, parameters = normalize_parameter_schema(parameter_schema)
        source_checksum = _source_checksum(source)
        now = _utc_now()
        connection = self._connection()
        try:
            _, owner = self._load_formula_row(connection, formula_id)
            self._load_draft_row(connection, formula_id, owner=owner)
            diagnostics = _formula_diagnostics(source, parameters, owner.formula_type)
            changed = connection.execute(
                update(FormulaDraftRow)
                .where(
                    FormulaDraftRow.formula_id == formula_id,
                    FormulaDraftRow.revision == expected_revision,
                )
                .values(
                    revision=expected_revision + 1,
                    source=source,
                    source_checksum=source_checksum,
                    parameter_schema_json=schema,
                    validation_result_json=list(diagnostics),
                    executable_version_id=None,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                exists = connection.execute(
                    select(FormulaRow.id).where(FormulaRow.id == formula_id)
                ).first()
                if exists is None:
                    raise FormulaNotFound("formula does not exist")
                raise FormulaConflict("formula draft changed concurrently")
            connection.commit()
        except BaseException:
            if connection.in_transaction():
                connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_draft(formula_id)

    def save(
        self,
        formula_id: str,
        source: str,
        parameter_schema: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> FormulaVersion:
        if type(expected_revision) is not int or expected_revision < 1:
            raise FormulaValidationError("draft revision is invalid")
        schema, parameters = normalize_parameter_schema(parameter_schema)
        source_checksum = _source_checksum(source)
        publication_rejected = False
        version_id = str(uuid4())
        connection = self._connection()
        try:
            formula_row, owner = self._load_formula_row(connection, formula_id)
            _, draft = self._load_draft_row(connection, formula_id, owner=owner)
            if draft.revision != expected_revision:
                raise FormulaConflict("formula draft changed concurrently")
            diagnostics = _formula_diagnostics(source, parameters, owner.formula_type)
            now = _utc_now()
            if diagnostics:
                changed = connection.execute(
                    update(FormulaDraftRow)
                    .where(
                        FormulaDraftRow.formula_id == formula_id,
                        FormulaDraftRow.revision == draft.revision,
                    )
                    .values(
                        revision=draft.revision + 1,
                        source=source,
                        source_checksum=source_checksum,
                        parameter_schema_json=schema,
                        validation_result_json=list(diagnostics),
                        executable_version_id=None,
                        updated_at=now,
                    )
                )
                if changed.rowcount != 1:
                    raise FormulaConflict("formula draft changed concurrently")
                connection.commit()
                publication_rejected = True
            else:
                version = owner.latest_version + 1
                changed = connection.execute(
                    update(FormulaRow)
                    .where(
                        FormulaRow.id == formula_id,
                        FormulaRow.latest_version == owner.latest_version,
                    )
                    .values(latest_version=version, updated_at=now)
                )
                if changed.rowcount != 1:
                    raise FormulaConflict("formula version changed concurrently")
                connection.execute(
                    insert(FormulaVersionRow),
                    self._version_values(
                        version_id,
                        formula_id,
                        version,
                        owner.name,
                        owner.formula_type,
                        owner.placement,
                        source,
                        schema,
                        now,
                    ),
                )
                changed = connection.execute(
                    update(FormulaDraftRow)
                    .where(
                        FormulaDraftRow.formula_id == formula_id,
                        FormulaDraftRow.revision == draft.revision,
                    )
                    .values(
                        revision=draft.revision + 1,
                        source=source,
                        source_checksum=source_checksum,
                        parameter_schema_json=schema,
                        validation_result_json=_published_report(
                            source_checksum, schema
                        ),
                        executable_version_id=version_id,
                        updated_at=now,
                    )
                )
                if changed.rowcount != 1:
                    raise FormulaConflict("formula draft changed concurrently")
                connection.commit()
        except BaseException:
            if connection.in_transaction():
                connection.rollback()
            raise
        finally:
            connection.close()
        if publication_rejected:
            raise FormulaValidationError("formula cannot be published")
        return self.get_version(version_id)

    def copy(
        self, formula_id: str, new_name: str, *, source_version_id: str | None = None
    ) -> FormulaVersion:
        connection = self._connection()
        try:
            _, owner = self._load_formula_row(connection, formula_id)
            resolved_version_id = source_version_id
            if resolved_version_id is None:
                resolved_version_id = connection.execute(
                    select(FormulaVersionRow.id).where(
                        FormulaVersionRow.formula_id == formula_id,
                        FormulaVersionRow.version == owner.latest_version,
                    )
                ).scalar_one_or_none()
            if resolved_version_id is None:
                raise FormulaNotFound("formula version does not exist")
            version_owner_id = connection.execute(
                select(FormulaVersionRow.formula_id).where(
                    FormulaVersionRow.id == resolved_version_id
                )
            ).scalar_one_or_none()
            if version_owner_id is None or version_owner_id != formula_id:
                raise FormulaNotFound("formula version does not exist")
            version_row, version = self._load_version_row(
                connection, resolved_version_id, owner=owner
            )
            if version.formula_id != formula_id:
                raise FormulaNotFound("formula version does not exist")
            _validate_identity(
                new_name,
                owner.formula_type,
                owner.placement,
            )
            new_formula_id, new_version_id, now = str(uuid4()), str(uuid4()), _utc_now()
            connection.execute(
                insert(FormulaRow),
                {
                    "id": new_formula_id,
                    "name": new_name,
                    "formula_type": owner.formula_type,
                    "placement": owner.placement,
                    "latest_version": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            schema = _canonical_json(
                version_row["parameter_schema_json"], "parameter schema"
            )
            validation_result = [dict(item) for item in version.validation_result]
            connection.execute(
                insert(FormulaVersionRow),
                {
                    "id": new_version_id,
                    "formula_id": new_formula_id,
                    "version": 1,
                    "name": new_name,
                    "formula_type": version.formula_type,
                    "placement": version.placement,
                    "source": version.source,
                    "parameter_schema_json": schema,
                    "compatibility_version": version.compatibility_version,
                    "engine_version": version.engine_version,
                    "checksum": version.checksum,
                    "validation_result_json": validation_result,
                    "copied_from_version_id": version.id,
                    "created_at": now,
                },
            )
            connection.execute(
                insert(FormulaDraftRow),
                {
                    "formula_id": new_formula_id,
                    "revision": 1,
                    "source": version.source,
                    "source_checksum": version.checksum,
                    "parameter_schema_json": schema,
                    "validation_result_json": validation_result,
                    "executable_version_id": new_version_id,
                    "updated_at": now,
                },
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction():
                connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_version(new_version_id)

    def _version_values(
        self,
        version_id: str,
        formula_id: str,
        version: int,
        name: str,
        formula_type: FormulaType,
        placement: FormulaPlacement,
        source: str,
        schema: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        source_checksum = _source_checksum(source)
        return {
            "id": version_id,
            "formula_id": formula_id,
            "version": version,
            "name": name,
            "formula_type": formula_type,
            "placement": placement,
            "source": source,
            "parameter_schema_json": schema,
            "compatibility_version": COMPATIBILITY_VERSION,
            "engine_version": ENGINE_VERSION,
            "checksum": source_checksum,
            "validation_result_json": _published_report(source_checksum, schema),
            "copied_from_version_id": None,
            "created_at": now,
        }

    def get_formula(self, formula_id: str) -> Formula:
        try:
            with self._read_connection() as connection:
                _, formula = self._load_formula_row(connection, formula_id)
                return formula
        except FormulaNotFound:
            raise
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def list_formula_page(
        self, *, limit: int, cursor: str | None
    ) -> tuple[tuple[Formula, ...], str | None]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FormulaValidationError("formula page limit is invalid")
        if cursor is not None and (
            type(cursor) is not str or not 0 < len(cursor) <= 128
        ):
            raise FormulaCursorError("formula cursor is invalid")
        try:
            with self._read_connection() as connection:
                statement = select(FormulaRow.__table__).order_by(
                    FormulaRow.created_at, FormulaRow.id
                )
                if cursor is not None:
                    cursor_row = (
                        connection.execute(
                            select(FormulaRow.created_at, FormulaRow.id).where(
                                FormulaRow.id == cursor
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if cursor_row is None:
                        raise FormulaCursorError("formula cursor is invalid")
                    statement = statement.where(
                        or_(
                            FormulaRow.created_at > cursor_row["created_at"],
                            and_(
                                FormulaRow.created_at == cursor_row["created_at"],
                                FormulaRow.id > cursor_row["id"],
                            ),
                        )
                    )
                rows = connection.execute(statement.limit(limit + 1)).mappings().all()
                formulas = self._formulas_from_rows(connection, list(rows[:limit]))
                next_cursor = (
                    formulas[-1].id if len(rows) > limit and formulas else None
                )
                return formulas, next_cursor
        except FormulaCursorError:
            raise
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def list_formulas(self) -> tuple[Formula, ...]:
        try:
            with self._read_connection() as connection:
                rows = (
                    connection.execute(
                        select(FormulaRow.__table__).order_by(
                            FormulaRow.created_at, FormulaRow.id
                        )
                    )
                    .mappings()
                    .all()
                )
                return self._formulas_from_rows(connection, list(rows))
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def get_draft(self, formula_id: str) -> FormulaDraft:
        try:
            with self._read_connection() as connection:
                _, draft = self._load_draft_row(connection, formula_id)
                return draft
        except FormulaNotFound:
            raise
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def get_version(self, version_id: str) -> FormulaVersion:
        try:
            with self._read_connection() as connection:
                _, version = self._load_version_row(connection, version_id)
                return version
        except FormulaNotFound:
            raise
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def list_version_page(
        self, formula_id: str, *, limit: int, cursor: str | None
    ) -> tuple[tuple[FormulaVersion, ...], str | None]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FormulaValidationError("formula page limit is invalid")
        if cursor is not None and (
            type(cursor) is not str or not 0 < len(cursor) <= 128
        ):
            raise FormulaCursorError("formula cursor is invalid")
        try:
            with self._read_connection() as connection:
                _, owner = self._load_formula_row(connection, formula_id)
                statement = (
                    select(FormulaVersionRow.__table__)
                    .where(FormulaVersionRow.formula_id == formula_id)
                    .order_by(FormulaVersionRow.version, FormulaVersionRow.id)
                )
                if cursor is not None:
                    cursor_row = (
                        connection.execute(
                            select(
                                FormulaVersionRow.version, FormulaVersionRow.id
                            ).where(
                                FormulaVersionRow.id == cursor,
                                FormulaVersionRow.formula_id == formula_id,
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if cursor_row is None:
                        raise FormulaCursorError("formula cursor is invalid")
                    statement = statement.where(
                        or_(
                            FormulaVersionRow.version > cursor_row["version"],
                            and_(
                                FormulaVersionRow.version == cursor_row["version"],
                                FormulaVersionRow.id > cursor_row["id"],
                            ),
                        )
                    )
                rows = connection.execute(statement.limit(limit + 1)).mappings().all()
                versions = tuple(_version(row, owner) for row in rows[:limit])
                next_cursor = (
                    versions[-1].id if len(rows) > limit and versions else None
                )
                return versions, next_cursor
        except (FormulaNotFound, FormulaCursorError):
            raise
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None

    def list_versions(self, formula_id: str) -> tuple[FormulaVersion, ...]:
        try:
            with self._read_connection() as connection:
                _, owner = self._load_formula_row(connection, formula_id)
                rows = (
                    connection.execute(
                        select(FormulaVersionRow.__table__)
                        .where(FormulaVersionRow.formula_id == formula_id)
                        .order_by(FormulaVersionRow.version, FormulaVersionRow.id)
                    )
                    .mappings()
                    .all()
                )
                return tuple(_version(row, owner) for row in rows)
        except FormulaNotFound:
            raise
        except FormulaRepositoryError:
            raise _catalog_corruption() from None
        except Exception:
            raise _catalog_corruption() from None
