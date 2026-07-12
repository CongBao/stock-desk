from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class GuidancePage(StrEnum):
    MARKET = "market"
    FORMULA = "formula"
    BACKTEST = "backtest"
    ANALYSIS = "analysis"
    TASKS = "tasks"


class GuidanceStatus(StrEnum):
    COMPLETED = "completed"
    DISMISSED = "dismissed"


class GuidancePagePreference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    content_version: int = Field(ge=1, le=2_147_483_647)
    status: GuidanceStatus


class GuidancePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    revision: int = Field(default=0, ge=0)
    pages: dict[GuidancePage, GuidancePagePreference] = Field(default_factory=dict)
