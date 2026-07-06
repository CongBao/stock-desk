from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, getcontext
import json
from pathlib import Path


getcontext().prec = 60
OUTPUT = Path(__file__).with_name("macd.json")


def prices() -> list[Decimal]:
    points: list[Decimal] = []
    value = Decimal("10")
    for change, count in (
        ("0.4", 18),
        ("-0.55", 18),
        ("0", 8),
        ("0.65", 18),
        ("-0.7", 18),
        ("0.5", 20),
    ):
        for _ in range(count):
            value += Decimal(change)
            points.append(value)
    return points


def ema(source: list[Decimal], n: int) -> list[Decimal]:
    result: list[Decimal] = []
    alpha = Decimal(2) / Decimal(n + 1)
    state: Decimal | None = None
    for value in source:
        state = value if state is None else alpha * value + (Decimal(1) - alpha) * state
        result.append(state)
    return result


def cross(left: list[Decimal], right: list[Decimal]) -> list[bool]:
    return [False] + [
        left[index] > right[index] and left[index - 1] <= right[index - 1]
        for index in range(1, len(left))
    ]


def rounded(values: list[Decimal]) -> list[float]:
    return [float(value.quantize(Decimal("0.000000000001"))) for value in values]


def canonical_timestamps(count: int) -> list[str]:
    """Weekday A-share day buckets; this intentionally is not a holiday calendar."""

    result: list[str] = []
    local_day = date(2024, 1, 2)
    while len(result) < count:
        if local_day.weekday() < 5:
            bucket = datetime.combine(
                local_day - timedelta(days=1), time(16), tzinfo=timezone.utc
            )
            result.append(bucket.isoformat().replace("+00:00", "Z"))
        local_day += timedelta(days=1)
    return result


def payload() -> dict[str, object]:
    close = prices()
    fast, slow = ema(close, 12), ema(close, 26)
    dif = [a - b for a, b in zip(fast, slow, strict=True)]
    dea = ema(dif, 9)
    macd = [(a - b) * 2 for a, b in zip(dif, dea, strict=True)]
    return {
        "compatibility_version": "tdx-v1",
        "ema_initialization": "first_valid_value",
        "input": {
            "timestamps": canonical_timestamps(len(close)),
            "close": [str(value) for value in close],
        },
        "expected": {
            "DIF": rounded(dif),
            "DEA": rounded(dea),
            "MACD": rounded(macd),
            "BUY": cross(dif, dea),
            "SELL": cross(dea, dif),
        },
    }


def render() -> str:
    return json.dumps(payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        return (
            0
            if OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == expected
            else 1
        )
    OUTPUT.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
