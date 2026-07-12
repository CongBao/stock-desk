from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stock_desk.market.types import (
    Adjustment,
    CanonicalSymbol,
    Exchange,
    InstrumentKind,
    Period,
    UtcDatetime,
)


WorkspacePage = Literal[
    "/market",
    "/formulas",
    "/backtests",
    "/analysis",
    "/tasks",
    "/settings",
]
WorkspaceNotice = Literal[
    "workspace_missing",
    "workspace_corrupt",
    "workspace_schema_unsupported",
    "workspace_expired",
    "workspace_route_invalid",
    "workspace_instrument_unavailable",
    "workspace_chart_unavailable",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


class WorkspaceInstrument(_FrozenModel):
    symbol: CanonicalSymbol
    name: Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S(?:.*\S)?$")]
    exchange: Exchange
    kind: InstrumentKind

    @classmethod
    def default(cls) -> WorkspaceInstrument:
        return cls(
            symbol="000001.SS",
            name="上证指数",
            exchange=Exchange.SH,
            kind=InstrumentKind.INDEX,
        )


class WorkspaceZoom(_FrozenModel):
    start: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0.0
    end: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 100.0

    @model_validator(mode="after")
    def validate_order(self) -> WorkspaceZoom:
        if self.start >= self.end:
            raise ValueError("workspace zoom start must precede end")
        return self


class EmptySubchart(_FrozenModel):
    kind: Literal["none"]


class VolumeSubchart(_FrozenModel):
    kind: Literal["volume"]


class FormulaSubchart(_FrozenModel):
    kind: Literal["formula"]
    formula_version_id: UUID


SubchartPreference: TypeAlias = Annotated[
    EmptySubchart | VolumeSubchart | FormulaSubchart,
    Field(discriminator="kind"),
]


class WorkspacePreferences(_FrozenModel):
    current_page: WorkspacePage = "/market"
    instrument: WorkspaceInstrument
    period: Period = Period.DAY
    adjustment: Adjustment = Adjustment.QFQ
    zoom: WorkspaceZoom = WorkspaceZoom()
    main_chart: Literal["candlestick"] = "candlestick"
    subchart: SubchartPreference = VolumeSubchart(kind="volume")

    @classmethod
    def safe_default(cls) -> WorkspacePreferences:
        return cls(instrument=WorkspaceInstrument.default())


class WorkspaceState(_FrozenModel):
    schema_version: Literal[1] = 1
    revision: Annotated[int, Field(ge=1)]
    updated_at: UtcDatetime
    preferences: WorkspacePreferences


class WorkspacePut(_FrozenModel):
    expected_revision: Annotated[int, Field(ge=0)]
    current_page: WorkspacePage
    instrument: WorkspaceInstrument
    period: Period
    adjustment: Adjustment
    zoom: WorkspaceZoom
    main_chart: Literal["candlestick"]
    subchart: SubchartPreference

    def preferences(self) -> WorkspacePreferences:
        return WorkspacePreferences.model_validate(
            self.model_dump(mode="python", exclude={"expected_revision"}),
            strict=True,
        )


class WorkspaceView(_FrozenModel):
    schema_version: Literal[1] = 1
    revision: Annotated[int, Field(ge=0)]
    updated_at: datetime | None
    expires_at: datetime | None
    restored: bool
    notice: WorkspaceNotice | None
    workspace: WorkspacePreferences
