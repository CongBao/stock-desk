from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.prepare_windows_packaged_backtest_evidence as prepare_module
import scripts.verify_packaged_backtest_evidence as verify_module


def test_prepare_rejects_invalid_identity_and_nonempty_destination(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="exact lowercase Git object ids"):
        prepare_module.prepare(
            tmp_path / "bad", source_sha="A" * 40, source_tree="b" * 40
        )

    destination = tmp_path / "nonempty"
    destination.mkdir()
    (destination / "keep").touch()
    with pytest.raises(ValueError, match="destination must be empty"):
        prepare_module.prepare(destination, source_sha="a" * 40, source_tree="b" * 40)


def test_switch_fixture_rejects_unknown_fixture_and_missing_database(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="unknown packaged fixture id"):
        prepare_module.switch_fixture(tmp_path, "unknown")
    with pytest.raises(ValueError, match="fixture database is missing"):
        prepare_module.switch_fixture(tmp_path, "matrix_1d")


def test_prepare_main_handles_prepare_and_switch_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared: list[tuple[Path, str, str]] = []
    switched: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        prepare_module,
        "prepare",
        lambda destination, *, source_sha, source_tree: (
            prepared.append((destination, source_sha, source_tree))
            or {"prepared": True}
        ),
    )
    monkeypatch.setattr(
        prepare_module,
        "switch_fixture",
        lambda destination, fixture: switched.append((destination, fixture)),
    )
    common = [
        "--destination",
        str(tmp_path / "data"),
        "--source-sha",
        "a" * 40,
        "--source-tree",
        "b" * 40,
    ]

    assert prepare_module.main(common) == 0
    assert json.loads(capsys.readouterr().out) == {"prepared": True}
    assert prepared == [((tmp_path / "data").resolve(), "a" * 40, "b" * 40)]

    assert prepare_module.main([*common, "--switch-fixture", "matrix_1d"]) == 0
    assert json.loads(capsys.readouterr().out) == {"fixture_id": "matrix_1d"}
    assert switched == [((tmp_path / "data").resolve(), "matrix_1d")]


@pytest.mark.parametrize("payload", (b"", b"not-json", b"[]"))
def test_verifier_load_rejects_unreadable_or_nonobject_payload(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(payload)

    with pytest.raises(verify_module.EvidenceError):
        verify_module._load(path)


def test_verifier_load_rejects_oversized_payload(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(verify_module.EvidenceError, match="invalid evidence size"):
        verify_module._load(path, maximum=1)


def test_verifier_record_rejects_missing_payload(tmp_path: Path) -> None:
    with pytest.raises(verify_module.EvidenceError, match="payload is unreadable"):
        verify_module._record(
            tmp_path / "missing", relative_path="missing", role="test"
        )


def _comparison() -> tuple[dict[str, object], dict[str, object]]:
    installer = "a" * 64
    bundle = {"manifest_sha256": "b" * 64}
    comparison: dict[str, object] = {
        "schema_version": 1,
        "artifact": "windows-desktop-reproducibility-comparison",
        "reproducible": True,
        "source_sha": "c" * 40,
        "left_manifest_sha256": bundle["manifest_sha256"],
        "right_manifest_sha256": "d" * 64,
        "left_installer_sha256": installer,
        "right_installer_sha256": "e" * 64,
        "nsis": {
            "equivalent": True,
            "allowed_differences": ["pe-checksum", "pe-timestamp"],
            "left_raw_sha256": installer,
            "right_raw_sha256": "e" * 64,
            "canonical_sha256": "f" * 64,
        },
    }
    return comparison, bundle


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda comparison: comparison.__setitem__("unexpected", True),
            "comparison fields are not canonical",
        ),
        (
            lambda comparison: comparison.__setitem__("source_sha", "wrong"),
            "comparison does not bind candidate A",
        ),
        (
            lambda comparison: comparison.__setitem__(
                "right_manifest_sha256", "invalid"
            ),
            "invalid right_manifest_sha256",
        ),
        (
            lambda comparison: comparison.__setitem__("nsis", None),
            "NSIS comparison is missing",
        ),
        (
            lambda comparison: comparison["nsis"].__setitem__("equivalent", False),
            "NSIS comparison is invalid",
        ),
        (
            lambda comparison: comparison["nsis"].__setitem__(
                "allowed_differences", ["pe-timestamp", "pe-checksum"]
            ),
            "NSIS comparison is invalid",
        ),
        (
            lambda comparison: comparison["nsis"].__setitem__(
                "allowed_differences", ["unsupported"]
            ),
            "NSIS comparison is invalid",
        ),
    ),
)
def test_comparison_validation_fails_closed(mutation: object, message: str) -> None:
    comparison, bundle = _comparison()
    mutation(comparison)  # type: ignore[operator]

    with pytest.raises(verify_module.EvidenceError, match=message):
        verify_module._validate_comparison(
            comparison,
            source_sha="c" * 40,
            bundle=bundle,
            installer_sha256="a" * 64,
        )


def test_create_promotion_translates_invalid_bundle_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify_module, "_load", lambda _path: {})
    monkeypatch.setattr(
        verify_module,
        "validate_bundle_manifest",
        lambda _value: (_ for _ in ()).throw(
            verify_module.BundleVerificationError("invalid")
        ),
    )
    missing = tmp_path / "missing"

    with pytest.raises(verify_module.EvidenceError, match="bundle manifest is invalid"):
        verify_module.create_promotion(
            root=tmp_path,
            installer_path=missing,
            bundle_manifest_path=missing,
            comparison_path=missing,
            evidence_path=missing,
            seed_path=missing,
            host_observation_path=missing,
            desktop_manifest_path=missing,
            source_sha="a" * 40,
            source_tree="b" * 40,
        )


def _verify_cli_args(tmp_path: Path) -> list[str]:
    return [
        str(tmp_path / "evidence.json"),
        "--seed",
        str(tmp_path / "seed.json"),
        "--host-observation",
        str(tmp_path / "host.json"),
        "--desktop-manifest",
        str(tmp_path / "desktop.json"),
        "--installer",
        str(tmp_path / "installer.exe"),
        "--bundle-manifest",
        str(tmp_path / "bundle.json"),
        "--source-sha",
        "a" * 40,
        "--source-tree",
        "b" * 40,
        "--candidate-sha256",
        "c" * 64,
    ]


def test_verifier_main_runs_verification_without_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        verify_module, "verify", lambda *args, **kwargs: calls.append((*args, kwargs))
    )

    assert verify_module.main(_verify_cli_args(tmp_path)) == 0
    assert len(calls) == 1


def test_verifier_main_rejects_partial_promotion_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify_module, "verify", lambda *_args, **_kwargs: None)

    with pytest.raises(verify_module.EvidenceError, match="required together"):
        verify_module.main(
            [
                *_verify_cli_args(tmp_path),
                "--comparison",
                str(tmp_path / "compare.json"),
            ]
        )


def test_verifier_main_creates_and_rechecks_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "promotion.json"
    promotion = {"binding": "complete"}
    verified: list[tuple[Path, Path, str, str]] = []
    monkeypatch.setattr(verify_module, "verify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verify_module, "create_promotion", lambda **_kwargs: promotion)
    monkeypatch.setattr(
        verify_module,
        "verify_promotion",
        lambda path, *, root, source_sha, source_tree: verified.append(
            (path, root, source_sha, source_tree)
        ),
    )
    root = tmp_path / "root"

    assert (
        verify_module.main(
            [
                *_verify_cli_args(tmp_path),
                "--comparison",
                str(tmp_path / "compare.json"),
                "--promotion-root",
                str(root),
                "--output-promotion",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_bytes() == verify_module.canonical_json(promotion)
    assert verified == [(output.resolve(), root.resolve(), "a" * 40, "b" * 40)]
    assert not (tmp_path / ".promotion.json.tmp").exists()
