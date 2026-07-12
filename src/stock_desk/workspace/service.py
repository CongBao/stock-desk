from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from stock_desk.formula.repository import FormulaRepository
from stock_desk.workspace.models import (
    FormulaSubchart,
    WorkspaceInstrument,
    WorkspaceNotice,
    WorkspacePreferences,
    WorkspacePut,
    WorkspaceState,
    WorkspaceView,
)
from stock_desk.workspace.store import WorkspaceStateStorageError, WorkspaceStateStore


WORKSPACE_TTL = timedelta(days=180)


class WorkspaceConflict(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Instruments(Protocol):
    def get(self, symbol: str) -> Any: ...


class _Market(Protocol):
    @property
    def instruments(self) -> _Instruments: ...


class WorkspaceService:
    """Restore only bounded preferences whose referenced objects still exist."""

    def __init__(
        self,
        *,
        store: WorkspaceStateStore,
        market: _Market | Callable[[], _Market],
        formula_repository: FormulaRepository
        | Callable[[], FormulaRepository]
        | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self._market_provider = market if callable(market) else lambda: market
        self._formula_provider = (
            formula_repository
            if formula_repository is None or callable(formula_repository)
            else lambda: formula_repository
        )
        self._clock = clock
        self._lock = RLock()

    @classmethod
    def open(
        cls,
        *,
        data_dir: Path,
        market: _Market | Callable[[], _Market],
        formula_repository: FormulaRepository
        | Callable[[], FormulaRepository]
        | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> WorkspaceService:
        return cls(
            store=WorkspaceStateStore(
                data_dir.resolve() / "workspace" / "state-v1.json"
            ),
            market=market,
            formula_repository=formula_repository,
            clock=clock,
        )

    def restore(self) -> WorkspaceView:
        with self._lock:
            try:
                state = self.store.load()
            except WorkspaceStateStorageError as error:
                return self._fallback(error.code)
            if state is None:
                return self._fallback("workspace_missing")
            now = self._clock()
            if state.updated_at > now + timedelta(minutes=5):
                return self._fallback("workspace_corrupt")
            if now - state.updated_at > WORKSPACE_TTL:
                return self._fallback("workspace_expired")
            if not self._instrument_exists(state.preferences.instrument):
                return self._fallback("workspace_instrument_unavailable")
            if not self._chart_exists(state.preferences):
                return self._fallback("workspace_chart_unavailable")
            return self._view(state, restored=True, notice=None)

    def update(self, request: WorkspacePut) -> WorkspaceView:
        with self._lock:
            try:
                current = self.store.load()
            except WorkspaceStateStorageError:
                current = None
            revision = 0 if current is None else current.revision
            if request.expected_revision != revision:
                raise WorkspaceConflict("workspace_revision_conflict")
            preferences = request.preferences()
            if not self._instrument_exists(preferences.instrument):
                raise WorkspaceConflict("workspace_instrument_unavailable")
            if not self._chart_exists(preferences):
                raise WorkspaceConflict("workspace_chart_unavailable")
            state = self.store.save(
                WorkspaceState(
                    revision=revision + 1,
                    updated_at=self._clock(),
                    preferences=preferences,
                )
            )
            return self._view(state, restored=True, notice=None)

    def initialize(self, instrument: WorkspaceInstrument) -> WorkspaceView:
        """Create the first committed workspace after successful onboarding."""
        with self._lock:
            try:
                current = self.store.load()
            except WorkspaceStateStorageError:
                current = None
            if current is not None:
                restored = self.restore()
                if restored.restored:
                    return restored
            preferences = WorkspacePreferences.safe_default().model_copy(
                update={"instrument": instrument}
            )
            if not self._instrument_exists(instrument):
                preferences = WorkspacePreferences.safe_default()
            state = self.store.save(
                WorkspaceState(
                    revision=1 if current is None else current.revision + 1,
                    updated_at=self._clock(),
                    preferences=preferences,
                )
            )
            return self._view(state, restored=True, notice=None)

    def delete(self) -> None:
        self.store.delete()

    def _instrument_exists(self, expected: WorkspaceInstrument) -> bool:
        if expected == WorkspaceInstrument.default():
            return True
        try:
            actual = self._market_provider().instruments.get(expected.symbol).instrument
        except Exception:
            return False
        return (
            actual.symbol == expected.symbol
            and actual.name == expected.name
            and actual.exchange is expected.exchange
            and actual.instrument_kind is expected.kind
        )

    def _chart_exists(self, preferences: WorkspacePreferences) -> bool:
        subchart = preferences.subchart
        if not isinstance(subchart, FormulaSubchart):
            return True
        if self._formula_provider is None:
            return False
        try:
            formula = self._formula_provider().get_version(
                str(subchart.formula_version_id)
            )
        except Exception:
            return False
        return formula.placement == "subchart"

    def _fallback(self, notice: WorkspaceNotice) -> WorkspaceView:
        return WorkspaceView(
            revision=0,
            updated_at=None,
            expires_at=None,
            restored=False,
            notice=notice,
            workspace=WorkspacePreferences.safe_default(),
        )

    @staticmethod
    def _view(
        state: WorkspaceState, *, restored: bool, notice: WorkspaceNotice | None
    ) -> WorkspaceView:
        return WorkspaceView(
            revision=state.revision,
            updated_at=state.updated_at,
            expires_at=state.updated_at + WORKSPACE_TTL,
            restored=restored,
            notice=notice,
            workspace=state.preferences,
        )
