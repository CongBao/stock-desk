"""Private subprocess entry point for bounded AKShare research calls."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import sys

from stock_desk.analysis.sources.base import normalize_research_table
from stock_desk.market.providers.base import ProviderNoData
from stock_desk.market.providers.sdk import import_optional_sdk, required_sdk_callable


_MAX_OUTPUT_BYTES = 262_144
_OPERATIONS = frozenset(
    {
        "stock_financial_analysis_indicator_em",
        "stock_individual_notice_report",
        "stock_news_em",
    }
)


def _emit(payload: dict[str, object], *, result_path: Path) -> None:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        encoded = '{"status":"invalid_response"}'
    with result_path.open("wb") as result:
        result.write(encoded.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        return 2
    result_path = Path(args[2])
    if not result_path.is_absolute() or len(str(result_path).encode("utf-8")) > 4_096:
        return 2
    if args[0] not in _OPERATIONS:
        _emit({"status": "invalid_response"}, result_path=result_path)
        return 2
    if len(args[1].encode("utf-8")) > 4_096:
        _emit({"status": "invalid_response"}, result_path=result_path)
        return 2
    try:
        kwargs = json.loads(args[1])
        if not isinstance(kwargs, dict) or any(type(key) is not str for key in kwargs):
            raise ValueError
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink), redirect_stderr(sink):
                module = import_optional_sdk("akshare")
                operation = required_sdk_callable(module, args[0])
                table = operation(**kwargs)
                rows = normalize_research_table(table)
    except ProviderNoData:
        _emit({"status": "no_data"}, result_path=result_path)
        return 1
    except Exception:
        _emit({"status": "invalid_response"}, result_path=result_path)
        return 1
    _emit({"status": "ok", "rows": rows}, result_path=result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
