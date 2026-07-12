from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stock_desk.market.provenance import Sha256Digest
from stock_desk.market.types import (
    CanonicalSymbol,
    Exchange,
    InstrumentKind,
    ProviderId,
    UtcDatetime,
)


class OnboardingStep(StrEnum):
    WELCOME = "welcome"
    DATA_PREPARATION = "data_preparation"
    INSTRUMENT_SELECTION = "instrument_selection"
    SYNCHRONIZATION = "synchronization"
    COMPLETED = "completed"


class OnboardingStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class SynchronizationStatus(StrEnum):
    IDLE = "idle"
    VERIFIED = "verified"
    FAILED = "failed"


OnboardingAction = Literal[
    "retry", "switch_provider", "advanced", "demo", "exit_demo"
]
FREE_PROVIDER_IDS = (ProviderId.AKSHARE, ProviderId.BAOSTOCK)
DEFAULT_SYMBOL: CanonicalSymbol = "000001.SS"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class OnboardingSource(_FrozenModel):
    id: ProviderId
    label: Annotated[str, Field(min_length=1, max_length=64)]
    catalog_manifest_record_id: Sha256Digest
    catalog_dataset_version: Sha256Digest
    data_cutoff: UtcDatetime

    @model_validator(mode="after")
    def validate_free_source(self) -> Self:
        if self.id not in FREE_PROVIDER_IDS:
            raise ValueError("onboarding source must be token-free")
        return self


class OnboardingInstrument(_FrozenModel):
    symbol: CanonicalSymbol
    name: Annotated[str, Field(min_length=1, max_length=255)]
    exchange: Exchange
    instrument_kind: InstrumentKind


class OnboardingSynchronization(_FrozenModel):
    status: SynchronizationStatus
    provider_id: ProviderId | None = None
    manifest_record_id: Sha256Digest | None = None
    dataset_version: Sha256Digest | None = None
    data_cutoff: UtcDatetime | None = None
    row_count: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        evidence = (
            self.provider_id,
            self.manifest_record_id,
            self.dataset_version,
            self.data_cutoff,
        )
        if self.status is SynchronizationStatus.VERIFIED:
            if any(value is None for value in evidence) or self.row_count < 1:
                raise ValueError("verified synchronization requires complete evidence")
            if self.provider_id not in FREE_PROVIDER_IDS:
                raise ValueError("verified synchronization source must be token-free")
        elif any(value is not None for value in evidence) or self.row_count != 0:
            raise ValueError("unverified synchronization cannot contain evidence")
        return self


class OnboardingError(_FrozenModel):
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    actions: tuple[OnboardingAction, ...]


class OnboardingState(_FrozenModel):
    schema_version: Literal[1] = 1
    revision: Annotated[int, Field(ge=0)] = 0
    status: OnboardingStatus = OnboardingStatus.PENDING
    current_step: OnboardingStep = OnboardingStep.WELCOME
    source: OnboardingSource | None = None
    instrument: OnboardingInstrument = OnboardingInstrument(
        symbol=DEFAULT_SYMBOL,
        name="上证指数",
        exchange=Exchange.SH,
        instrument_kind=InstrumentKind.INDEX,
    )
    sync: OnboardingSynchronization | None = None
    error: OnboardingError | None = None
    demo_mode: bool = False
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def validate_state_machine(self) -> Self:
        if self.status is OnboardingStatus.COMPLETED:
            if (
                self.current_step is not OnboardingStep.COMPLETED
                or self.demo_mode
                or self.source is None
                or self.sync is None
                or self.sync.status is not SynchronizationStatus.VERIFIED
                or self.sync.provider_id is not self.source.id
            ):
                raise ValueError("completed onboarding requires verified real data")
        elif self.current_step is OnboardingStep.COMPLETED:
            raise ValueError("completed step requires completed status")
        if self.demo_mode and self.status is OnboardingStatus.COMPLETED:
            raise ValueError("demo mode cannot complete onboarding")
        return self

    def evolved(self, *, now: datetime, **changes: object) -> OnboardingState:
        return self.model_copy(
            update={"revision": self.revision + 1, "updated_at": now, **changes}
        )


class OnboardingSourceOption(_FrozenModel):
    id: ProviderId
    label: str
    token_free: Literal[True] = True
    selected: bool
