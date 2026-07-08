from __future__ import annotations

from pathlib import Path

from tests.unit.api.test_source_settings import DEFAULT_PRIORITIES, settings_api
from tests.unit.market.providers.tdx_test_helpers import (
    make_vipdoc_root,
    raw_record,
    write_tdx_file,
)


def _settings_payload(root: Path) -> dict[str, object]:
    priorities = {key: list(value) for key, value in DEFAULT_PRIORITIES.items()}
    priorities["daily_bars"] = [
        "tdx_local",
        *(source for source in priorities["daily_bars"] if source != "tdx_local"),
    ]
    return {"priorities": priorities, "tdx_path": str(root)}


def test_valid_tdx_directory_shows_markets_period_and_data_cutoff(
    tmp_path: Path,
) -> None:
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, "600000.SH", raw_record(raw_date=20240701))
    write_tdx_file(root, "000001.SZ", raw_record(raw_date=20240702))

    with settings_api(tmp_path, master_key=None) as context:
        saved = context.client.put(
            "/api/settings/sources", json=_settings_payload(root)
        )
        diagnostic = context.client.post("/api/settings/sources/tdx_local/test")

    assert saved.status_code == 200
    assert diagnostic.status_code == 200
    assert diagnostic.json() == {
        "source": "tdx_local",
        "status": "available",
        "capabilities": ["bars"],
        "permissions": [
            {"category": "minute_bars", "state": "unsupported"},
            {"category": "daily_bars", "state": "available"},
            {"category": "weekly_bars", "state": "unsupported"},
            {"category": "instruments", "state": "unsupported"},
            {"category": "trading_calendar", "state": "unsupported"},
            {"category": "execution_status", "state": "unsupported"},
        ],
        "available_periods": ["1d"],
        "markets": ["SH", "SZ"],
        "gaps": [
            {
                "category": "minute_bars",
                "state": "unsupported",
                "reason": "unsupported",
                "detail": "provider does not support 60-minute bars",
            },
            {
                "category": "weekly_bars",
                "state": "unsupported",
                "reason": "unsupported",
                "detail": "provider does not support weekly bars",
            },
            {
                "category": "instruments",
                "state": "unsupported",
                "reason": "unsupported",
                "detail": "provider does not support instruments",
            },
            {
                "category": "trading_calendar",
                "state": "unsupported",
                "reason": "unsupported",
                "detail": "provider does not support trading calendar",
            },
            {
                "category": "execution_status",
                "state": "unsupported",
                "reason": "unsupported",
                "detail": "provider does not support authoritative execution status",
            },
        ],
        "last_checked": "2026-07-06T09:30:00Z",
        "last_update": None,
        "data_cutoff": "2024-07-02T07:00:00Z",
        "fallback_reason": None,
    }


def test_unsupported_tdx_file_format_is_rejected_before_enablement(
    tmp_path: Path,
) -> None:
    root = make_vipdoc_root(tmp_path)
    target = write_tdx_file(root, "600000.SH", b"\x00" * 32)

    with settings_api(tmp_path, master_key=None) as context:
        saved = context.client.put(
            "/api/settings/sources", json=_settings_payload(root)
        )
        rejected = context.client.post("/api/settings/sources/tdx_local/test")
        target.write_bytes(raw_record(raw_date=20240701))
        recovered = context.client.post("/api/settings/sources/tdx_local/test")

    assert saved.status_code == 200
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "unavailable"
    assert rejected.json()["capabilities"] == []
    assert rejected.json()["available_periods"] == []
    assert rejected.json()["markets"] == []
    assert rejected.json()["fallback_reason"] == {
        "reason": "corrupt",
        "detail": "TDX vipdoc contents are corrupt",
    }
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "available"
    assert recovered.json()["markets"] == ["SH"]
    assert recovered.json()["data_cutoff"] == "2024-07-01T07:00:00Z"
