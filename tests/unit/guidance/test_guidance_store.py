from pathlib import Path

import pytest

from stock_desk.guidance.models import GuidancePage, GuidanceStatus
from stock_desk.guidance.store import (
    GuidancePreferencesConflict,
    GuidancePreferencesStore,
)


def test_preferences_are_versioned_persisted_and_compare_and_swapped(
    tmp_path: Path,
) -> None:
    store = GuidancePreferencesStore(tmp_path / "guidance" / "preferences.json")

    initial = store.load()
    assert initial.schema_version == 1
    assert initial.revision == 0
    assert initial.pages == {}

    saved = store.update(
        expected_revision=0,
        page=GuidancePage.MARKET,
        content_version=1,
        status=GuidanceStatus.COMPLETED,
    )
    assert saved.revision == 1
    assert saved.pages[GuidancePage.MARKET].content_version == 1

    reloaded = GuidancePreferencesStore(
        tmp_path / "guidance" / "preferences.json"
    ).load()
    assert reloaded == saved

    with pytest.raises(GuidancePreferencesConflict):
        store.update(
            expected_revision=0,
            page=GuidancePage.FORMULA,
            content_version=1,
            status=GuidanceStatus.DISMISSED,
        )


def test_content_version_is_scoped_to_one_page(tmp_path: Path) -> None:
    store = GuidancePreferencesStore(tmp_path / "preferences.json")
    first = store.update(
        expected_revision=0,
        page=GuidancePage.MARKET,
        content_version=1,
        status=GuidanceStatus.COMPLETED,
    )
    second = store.update(
        expected_revision=first.revision,
        page=GuidancePage.FORMULA,
        content_version=2,
        status=GuidanceStatus.DISMISSED,
    )

    assert second.pages[GuidancePage.MARKET].content_version == 1
    assert second.pages[GuidancePage.FORMULA].content_version == 2
