from __future__ import annotations

from dataclasses import dataclass

from stock_desk.formula.values import SeriesValue


@dataclass(frozen=True, slots=True)
class RuntimeIssue:
    code: str
    count: int
    first_index: int


@dataclass(frozen=True, slots=True)
class KernelResult:
    value: SeriesValue
    issues: tuple[RuntimeIssue, ...] = ()
