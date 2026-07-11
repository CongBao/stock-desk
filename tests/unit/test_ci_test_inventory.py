from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts import ci_test_inventory as inventory


SHA = "a" * 40
TREE = "b" * 40
NODEIDS = (
    "tests/unit/test_sample.py::test_unit",
    "tests/contract/test_sample.py::test_contract",
    "tests/property/test_sample.py::test_property",
    "tests/integration/test_sample.py::test_integration",
    "tests/acceptance/test_sample.py::test_acceptance",
    "tests/performance/test_sample.py::test_performance",
    "tests/security/test_sample.py::test_security",
)


def test_complete_inventory_assigns_every_nodeid_to_exactly_one_shard() -> None:
    payload = inventory.build_inventory(NODEIDS, source_sha=SHA, source_tree=TREE)

    assert payload["total_count"] == len(NODEIDS)
    assert payload["shards"]["unit"]["count"] == 3
    assert payload["shards"]["integration"]["count"] == 1
    assert payload["shards"]["acceptance-performance"]["count"] == 2
    assert payload["shards"]["security"]["count"] == 1
    assert inventory.validate_inventory(payload, source_sha=SHA, source_tree=TREE)


@pytest.mark.parametrize(
    "nodeid",
    [
        "tests/unknown/test_sample.py::test_unknown",
        "../tests/unit/test_sample.py::test_escape",
        "tests/unit/test_sample.py::TestOnly",
    ],
)
def test_unknown_escaping_and_non_function_nodeids_fail_closed(nodeid: str) -> None:
    with pytest.raises(inventory.InventoryError):
        inventory.build_inventory([nodeid], source_sha=SHA, source_tree=TREE)


def test_duplicate_ownership_and_inventory_tampering_are_rejected() -> None:
    with pytest.raises(inventory.InventoryError, match="duplicate nodeid"):
        inventory.build_inventory(
            [NODEIDS[0], NODEIDS[0]], source_sha=SHA, source_tree=TREE
        )

    payload = inventory.build_inventory(NODEIDS, source_sha=SHA, source_tree=TREE)
    changed = copy.deepcopy(payload)
    changed["shards"]["unit"]["nodeids"].pop()
    with pytest.raises(inventory.InventoryError, match="count does not match"):
        inventory.validate_inventory(changed)


def test_collection_parser_is_deterministic_and_rejects_duplicate_collection() -> None:
    output = "\n".join([NODEIDS[1], NODEIDS[0], "2 tests collected in 0.01s"])
    assert inventory.parse_collection_output(output) == tuple(sorted(NODEIDS[:2]))

    with pytest.raises(inventory.InventoryError, match="duplicate"):
        inventory.parse_collection_output(f"{NODEIDS[0]}\n{NODEIDS[0]}\n")


def test_inventory_requires_exact_sha_and_tree_identity() -> None:
    payload = inventory.build_inventory(NODEIDS, source_sha=SHA, source_tree=TREE)

    with pytest.raises(inventory.InventoryError, match="source_sha does not match"):
        inventory.validate_inventory(payload, source_sha="c" * 40)
    with pytest.raises(inventory.InventoryError, match="source_tree does not match"):
        inventory.validate_inventory(payload, source_tree="d" * 40)


@dataclass
class _FakeItem:
    nodeid: str
    user_properties: list[tuple[str, str]] = field(default_factory=list)


def test_pytest_plugin_embeds_complete_exact_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STOCK_DESK_SOURCE_SHA", SHA)
    monkeypatch.setenv("STOCK_DESK_SOURCE_TREE", TREE)
    monkeypatch.setenv("STOCK_DESK_PYTHON_SHARD", "unit")
    item = _FakeItem(NODEIDS[0])

    inventory.pytest_collection_modifyitems([item])

    assert item.user_properties == [
        ("stock_desk_nodeid", NODEIDS[0]),
        ("stock_desk_source_sha", SHA),
        ("stock_desk_source_tree", TREE),
        ("stock_desk_shard", "unit"),
    ]

    monkeypatch.delenv("STOCK_DESK_SOURCE_TREE")
    with pytest.raises(inventory.InventoryError, match="requires source SHA"):
        inventory.pytest_collection_modifyitems([_FakeItem(NODEIDS[0])])


def test_inventory_collect_and_verify_cli_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "inventory.json"
    monkeypatch.setattr(inventory, "collect_nodeids", lambda _root: NODEIDS)

    assert (
        inventory.main(
            [
                "collect",
                "--repo-root",
                str(tmp_path),
                "--source-sha",
                SHA,
                "--source-tree",
                TREE,
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert "uniquely-owned" in capsys.readouterr().out
    assert (
        inventory.main(
            [
                "verify",
                "--inventory",
                str(output),
                "--source-sha",
                SHA,
                "--source-tree",
                TREE,
            ]
        )
        == 0
    )

    output.write_text("{}\n", encoding="utf-8")
    assert inventory.main(["verify", "--inventory", str(output)]) == 1
    assert "pytest inventory error" in capsys.readouterr().err
