from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from stock_desk.storage.base import Base


FormulaType = Literal["indicator", "trading"]
FormulaPlacement = Literal["main", "subchart"]


class FormulaRow(Base):
    __tablename__ = "formula"
    __table_args__ = (
        CheckConstraint(
            "formula_type IN ('indicator','trading')", name="ck_formula_type"
        ),
        CheckConstraint(
            "placement IN ('main','subchart')", name="ck_formula_placement"
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 64 AND trim(name) = name",
            name="ck_formula_name",
        ),
        CheckConstraint("latest_version >= 0", name="ck_formula_latest_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    formula_type: Mapped[str] = mapped_column(String(16), nullable=False)
    placement: Mapped[str] = mapped_column(String(16), nullable=False)
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FormulaVersionRow(Base):
    __tablename__ = "formula_version"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_formula_version_positive"),
        CheckConstraint(
            "formula_type IN ('indicator','trading')", name="ck_formula_version_type"
        ),
        CheckConstraint(
            "placement IN ('main','subchart')", name="ck_formula_version_placement"
        ),
        CheckConstraint(
            "length(CAST(source AS BLOB)) BETWEEN 1 AND 64000",
            name="ck_formula_version_source",
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 64 AND trim(name) = name",
            name="ck_formula_version_name",
        ),
        CheckConstraint(
            "length(checksum) = 71 AND checksum LIKE 'sha256:%'",
            name="ck_formula_version_checksum",
        ),
        UniqueConstraint("formula_id", "version", name="uq_formula_version_number"),
        UniqueConstraint("formula_id", "id", name="uq_formula_version_owner_identity"),
        Index("ix_formula_version_formula", "formula_id", "version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    formula_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("formula.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    formula_type: Mapped[str] = mapped_column(String(16), nullable=False)
    placement: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    parameter_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    compatibility_version: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    validation_result_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    copied_from_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("formula_version.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FormulaDraftRow(Base):
    __tablename__ = "formula_draft"
    __table_args__ = (
        CheckConstraint(
            "length(CAST(source AS BLOB)) BETWEEN 1 AND 64000",
            name="ck_formula_draft_source",
        ),
        CheckConstraint(
            "length(source_checksum) = 71 AND source_checksum LIKE 'sha256:%'",
            name="ck_formula_draft_checksum",
        ),
        CheckConstraint("revision > 0", name="ck_formula_draft_revision"),
        ForeignKeyConstraint(
            ["formula_id", "executable_version_id"],
            ["formula_version.formula_id", "formula_version.id"],
            ondelete="RESTRICT",
        ),
    )

    formula_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("formula.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    parameter_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    validation_result_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    executable_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


@dataclass(frozen=True, slots=True)
class Formula:
    id: str
    name: str
    formula_type: FormulaType
    placement: FormulaPlacement
    latest_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FormulaDraft:
    formula_id: str
    revision: int
    source: str
    source_checksum: str
    parameter_schema: Mapping[str, Any]
    validation_result: tuple[Mapping[str, Any], ...]
    executable_version_id: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FormulaVersion:
    id: str
    formula_id: str
    version: int
    name: str
    formula_type: FormulaType
    placement: FormulaPlacement
    source: str
    parameter_schema: Mapping[str, Any]
    compatibility_version: str
    engine_version: str
    checksum: str
    validation_result: tuple[Mapping[str, Any], ...]
    copied_from_version_id: str | None
    created_at: datetime
