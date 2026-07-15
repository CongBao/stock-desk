from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast
from collections.abc import Iterator, Mapping

import pytest

from scripts import nsis_repack_contract as contract


SHA = "a" * 40
TREE = "b" * 40
EPOCH = 1_700_000_000
INSTALLER = b"unsigned-installer\n"
PLUGIN_PATH = "toolchain/Plugins/x86-unicode/additional/nsis_tauri_utils.dll"
NSISDL_PLUGIN_PATH = "toolchain/Plugins/x86-unicode/NSISdl.dll"
REQUIRED_TOOL_PATHS = sorted(contract._REQUIRED_TOOLCHAIN_PATHS)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _install_fixture_toolchain_lock(
    tmp_path: Path, files: list[dict[str, object]]
) -> None:
    tree = contract._canonical_toolchain_tree(files)
    lock = {
        "schema_version": 1,
        "tauri_cli": {
            "version": "2.11.4",
            "source_tag": "tauri-cli-v2.11.4",
            "source_path": "crates/tauri-bundler/src/bundle/windows/nsis/mod.rs",
        },
        "nsis": {
            "version": "3.11",
            "url": "https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip",
            "sha1": "ef7ff767e5cbd9edd22add3a32c9b8f4500bb10d",
            "sha256": "c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1",
        },
        "nsis_tauri_utils": {
            "version": "0.5.3",
            "url": "https://github.com/tauri-apps/nsis-tauri-utils/releases/download/nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll",
            "sha1": "75197fee3c6a814fe035788d1c34ead39349b860",
            "sha256": "5ba143b5db4a87d32d6e7802e033330aae56cbceabe0d1e3ba41948385ad4709",
        },
        "extracted_tree": tree,
    }
    path = tmp_path / "fixture-nsis-toolchain-lock.json"
    path.write_bytes(json.dumps(lock, indent=2).encode() + b"\n")
    setattr(contract, "TOOLCHAIN_LOCK_PATH", path)


def _fixture(tmp_path: Path, *, prefix: str = "A") -> tuple[Path, dict[str, Any]]:
    root = tmp_path / f"source-{prefix}"
    root.mkdir(parents=True)
    absolute_payload = f"C:\\runner\\{prefix}\\stock-desk.exe"
    absolute_plugins = f"C:\\runner\\{prefix}\\plugins"
    absolute_hook = f"C:\\runner\\{prefix}\\installer-hooks.nsh"
    rendered = (
        f'!define MAIN "{absolute_payload}"\n'
        f'!define ADDITIONALPLUGINSPATH "{absolute_plugins}"\n'
        f'!define HOOK "{absolute_hook}"\n'
        '!addplugindir "${ADDITIONALPLUGINSPATH}"\n'
        '!include "utils.nsh"\n'
        '!include "FileAssociation.nsh"\n'
        '!include "${HOOK}"\n'
        'File "${MAIN}"\n'
        'File "/oname=$TEMP\\stock-desk.exe" "payload/stock-desk.exe"\n'
        '${GetOptions} $CMDLINE "/P" $PassiveMode\n'
        '${GetSize} "$INSTDIR" "/M=uninstall.exe /S=0K /G=0" $0 $1 $2\n'
        'NSISdl::download "https://go.microsoft.com/fwlink/p/?LinkId=2124703" '
        '"$TEMP\\MicrosoftEdgeWebView2RuntimeInstaller.exe"\n'
        "nsis_tauri_utils::SemverCompare\n"
        'OutFile "unsigned/stock-desk.exe"\n'
    ).encode()
    contents: dict[str, tuple[str, bytes, bool]] = {
        "installer.nsi": ("nsis-rendered-script", rendered, False),
        "FileAssociation.nsh": ("nsis-include", b"; associations\n", False),
        "utils.nsh": ("nsis-include", b"; utilities\n", False),
        "packaging/installer.nsi.hbs": ("nsis-template", b"template\n", False),
        "packaging/installer-hooks.nsh": ("nsis-hook", b"; hook\n", False),
        "languages/SimpChinese.nsh": ("nsis-language", b"; language\n", False),
        "icons/app.ico": ("icon", b"icon\n", False),
        "runtime/WebView2.exe": ("webview2", b"webview\n", False),
        "payload/stock-desk.exe": ("payload", b"application\n", False),
        "tauri.conf.json": ("tauri-config", b"{}\n", False),
        NSISDL_PLUGIN_PATH: ("nsis-plugin", b"nsisdl-plugin\n", False),
    }
    for path in REQUIRED_TOOL_PATHS:
        if path == "toolchain/makensis.exe":
            payload = (
                b"#!/bin/sh\nprintf 'unsigned-installer\\n' > unsigned/stock-desk.exe\n"
            )
            contents[path] = ("nsis-toolchain", payload, True)
        elif path == PLUGIN_PATH:
            contents[path] = ("nsis-plugin", b"plugin\n", False)
        else:
            contents[path] = ("nsis-toolchain", f"{path}\n".encode(), False)
    for relative, (_role, payload, executable) in contents.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        destination.chmod(0o700 if executable else 0o600)
    files = [
        {
            "path": path,
            "role": role,
            "size": len(payload),
            "sha256": _digest(payload),
            "executable": executable,
        }
        for path, (role, payload, executable) in sorted(contents.items())
    ]
    _install_fixture_toolchain_lock(tmp_path, files)
    tool = contents["toolchain/makensis.exe"][1]
    plugin = contents[PLUGIN_PATH][1]
    nsisdl_plugin = contents[NSISDL_PLUGIN_PATH][1]
    descriptor: dict[str, Any] = {
        "schema_version": 1,
        "source_sha": SHA,
        "source_tree": TREE,
        "source_epoch": EPOCH,
        "toolchain": {
            "path": "toolchain/makensis.exe",
            "sha256": _digest(tool),
            "tauri_cli_version": "2.11.4",
            "nsis_version": "3.11",
            "nsis_tauri_utils_version": "0.5.3",
            "plugins": [
                {
                    "name": "nsis_tauri_utils",
                    "path": PLUGIN_PATH,
                    "sha256": _digest(plugin),
                },
                {
                    "name": "NSISdl",
                    "path": NSISDL_PLUGIN_PATH,
                    "sha256": _digest(nsisdl_plugin),
                },
            ],
        },
        "argv": [
            "-INPUTCHARSET",
            "UTF8",
            "-OUTPUTCHARSET",
            "UTF8",
            "-V3",
            "installer.nsi",
        ],
        "environment": {
            "SOURCE_DATE_EPOCH": str(EPOCH),
            "TZ": "UTC",
        },
        "cleared_environment": ["NSISCONFDIR", "NSISDIR"],
        "files": files,
        "expected_unsigned_installer": {
            "path": "unsigned/stock-desk.exe",
            "size": len(INSTALLER),
            "sha256": _digest(INSTALLER),
        },
        "path_mappings": [
            {
                "source_absolute": absolute_payload,
                "target": "payload/stock-desk.exe",
                "occurrences": 1,
            },
            {
                "source_absolute": absolute_plugins,
                "target": str(Path(PLUGIN_PATH).parent).replace("\\", "/"),
                "occurrences": 1,
            },
            {
                "source_absolute": absolute_hook,
                "target": "packaging/installer-hooks.nsh",
                "occurrences": 1,
            },
        ],
    }
    return root, descriptor


def _extracted_toolchain_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    nsis_root = tmp_path / "extracted-nsis"
    external_plugin = tmp_path / "external-plugins" / "nsis_tauri_utils.dll"
    records: list[dict[str, object]] = []
    for locked_path in REQUIRED_TOOL_PATHS:
        if locked_path == PLUGIN_PATH:
            destination = external_plugin
            payload = b"locked-external-plugin\n"
            role = "nsis-plugin"
        else:
            relative = locked_path.removeprefix("toolchain/")
            destination = nsis_root / relative
            payload = f"{locked_path}\n".encode()
            role = "nsis-toolchain"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        records.append(
            {
                "path": locked_path,
                "role": role,
                "size": len(payload),
                "sha256": _digest(payload),
                "executable": locked_path == "toolchain/makensis.exe",
            }
        )
    rendered_script = tmp_path / "render" / "installer.nsi"
    rendered_script.parent.mkdir(parents=True)
    rendered_script.write_text(
        f'!addplugindir "{external_plugin.parent}"\nnsis_tauri_utils::SemverCompare\n',
        encoding="utf-8",
    )
    _install_fixture_toolchain_lock(tmp_path, records)
    return nsis_root, rendered_script, external_plugin


def test_extracted_toolchain_verifier_accepts_the_locked_tree_and_external_plugin(
    tmp_path: Path,
) -> None:
    nsis_root, rendered_script, _external_plugin = _extracted_toolchain_fixture(
        tmp_path
    )
    verifier = getattr(contract, "verify_extracted_nsis_toolchain", None)

    assert callable(verifier)
    assert verifier(nsis_root, rendered_script) is not None


@pytest.mark.parametrize("tamper", ["compiler", "external-plugin", "tree"])
def test_extracted_toolchain_verifier_rejects_locked_identity_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    nsis_root, rendered_script, external_plugin = _extracted_toolchain_fixture(tmp_path)
    if tamper == "compiler":
        (nsis_root / "makensis.exe").write_bytes(b"tampered-compiler")
    elif tamper == "external-plugin":
        external_plugin.write_bytes(b"tampered-plugin")
    elif tamper == "tree":
        (nsis_root / "Include" / "unlocked.nsh").write_bytes(b"extra")
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(tamper)

    verifier = getattr(contract, "verify_extracted_nsis_toolchain", None)
    assert callable(verifier)
    with pytest.raises(contract.NsisRepackContractError):
        verifier(nsis_root, rendered_script)


@pytest.mark.parametrize(
    "unsafe_object",
    [
        "compiler-symlink",
        "compiler-hardlink",
        "compiler-reparse",
        "plugin-symlink",
        "plugin-hardlink",
        "plugin-reparse",
    ],
)
def test_extracted_toolchain_verifier_rejects_linked_or_reparse_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_object: str,
) -> None:
    nsis_root, rendered_script, external_plugin = _extracted_toolchain_fixture(tmp_path)
    target = (
        nsis_root / "makensis.exe"
        if unsafe_object.startswith("compiler-")
        else external_plugin
    )
    object_kind = unsafe_object.rpartition("-")[2]
    if object_kind in {"symlink", "hardlink"}:
        backing = tmp_path / "outside-backing" / target.name
        backing.parent.mkdir()
        target.rename(backing)
        if object_kind == "symlink":
            try:
                target.symlink_to(backing)
            except OSError:
                pytest.skip("file symlink creation is unavailable")
        else:
            os.link(backing, target)
    else:
        real_lstat = os.lstat

        def reparse_lstat(
            path: os.PathLike[str] | str, *args: object, **kwargs: object
        ):
            metadata = real_lstat(path, *args, **kwargs)
            if Path(path) != target:
                return metadata
            values = {
                name: getattr(metadata, name)
                for name in dir(metadata)
                if name.startswith("st_")
            }
            values["st_file_attributes"] = getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            )
            return SimpleNamespace(**values)

        monkeypatch.setattr(contract.os, "lstat", reparse_lstat)

    verifier = getattr(contract, "verify_extracted_nsis_toolchain", None)
    assert callable(verifier)
    with pytest.raises(contract.NsisRepackContractError):
        verifier(nsis_root, rendered_script)


def _write_descriptor(
    tmp_path: Path, value: object, name: str = "descriptor.json"
) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _create_kit(
    *,
    descriptor: Path,
    source_root: Path,
    output: Path,
    expected_source_sha: str = SHA,
    expected_source_tree: str = TREE,
) -> dict[str, object]:
    return contract.create_kit(
        descriptor=descriptor,
        source_root=source_root,
        output=output,
        expected_source_sha=expected_source_sha,
        expected_source_tree=expected_source_tree,
    )


def _create(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    source, descriptor = _fixture(tmp_path)
    kit = tmp_path / "kit"
    result = _create_kit(
        descriptor=_write_descriptor(tmp_path, descriptor),
        source_root=source,
        output=kit,
    )
    return kit, result


def test_create_kit_requires_external_source_identity_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    source, descriptor = _fixture(tmp_path)

    with pytest.raises(contract.NsisRepackContractError, match="expected_source_sha"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
            expected_source_sha="c" * 40,
            expected_source_tree=TREE,
        )


def test_create_kit_publication_never_uses_a_replacing_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, descriptor = _fixture(tmp_path)

    def reject_replace(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("replacing rename could overwrite a raced output")

    monkeypatch.setattr(contract.os, "replace", reject_replace)
    result = _create_kit(
        descriptor=_write_descriptor(tmp_path, descriptor),
        source_root=source,
        output=tmp_path / "kit",
    )

    assert result["artifact"] == contract.KIT_ARTIFACT


def test_create_kit_preserves_safe_private_lease_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, descriptor = _fixture(tmp_path)

    @contextmanager
    def fail_lease(_path: Path) -> Iterator[Path]:
        raise contract.SecureArtifactSnapshotError(
            "lease verification failed (Windows error 32)"
        )
        yield  # pragma: no cover - required by the contextmanager protocol

    monkeypatch.setattr(contract, "private_directory_lease", fail_lease)

    with pytest.raises(
        contract.NsisRepackContractError,
        match=(
            r"private kit root failed during lease creation: "
            r"lease verification failed \(Windows error 32\)"
        ),
    ):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


def _repack(kit: Path, output: Path, receipt: Path) -> dict[str, object]:
    manifest = json.loads((kit / contract.KIT_MANIFEST).read_bytes())
    return contract.repack(
        kit=kit,
        output=output,
        receipt=receipt,
        expected_source_sha=str(manifest["source_sha"]),
        expected_source_tree=str(manifest["source_tree"]),
        expected_kit_sha256=str(manifest["kit_sha256"]),
    )


def _verify_kit(
    *,
    kit: Path,
    expected_source_sha: str = SHA,
    expected_source_tree: str = TREE,
    expected_kit_sha256: str | None = None,
) -> dict[str, object]:
    manifest = json.loads((kit / contract.KIT_MANIFEST).read_bytes())
    return contract.verify_kit(
        kit=kit,
        expected_source_sha=expected_source_sha,
        expected_source_tree=expected_source_tree,
        expected_kit_sha256=expected_kit_sha256 or str(manifest["kit_sha256"]),
    )


def _verify_receipt(
    *,
    receipt: Path,
    kit: Path,
    output: Path,
    expected_source_sha: str = SHA,
    expected_source_tree: str = TREE,
    expected_kit_sha256: str | None = None,
) -> dict[str, object]:
    manifest = json.loads((kit / contract.KIT_MANIFEST).read_bytes())
    return contract.verify_receipt(
        receipt=receipt,
        kit=kit,
        output=output,
        expected_source_sha=expected_source_sha,
        expected_source_tree=expected_source_tree,
        expected_kit_sha256=expected_kit_sha256 or str(manifest["kit_sha256"]),
    )


def test_create_and_verify_content_addressed_kit(tmp_path: Path) -> None:
    kit, created = _create(tmp_path)

    verified = _verify_kit(kit=kit, expected_source_sha=SHA, expected_source_tree=TREE)

    assert verified == created
    assert set(path.name for path in kit.iterdir()) == {
        "content",
        contract.KIT_MANIFEST,
    }
    assert created["artifact"] == contract.KIT_ARTIFACT
    assert created["cleared_environment"] == ["NSISCONFDIR", "NSISDIR"]
    assert created["environment"] == {
        "SOURCE_DATE_EPOCH": str(EPOCH),
        "TEMP": contract._PRIVATE_WORK_PLACEHOLDER,
        "TMP": contract._PRIVATE_WORK_PLACEHOLDER,
        "TZ": "UTC",
    }
    normalization = cast(Mapping[str, object], created["normalization"])
    assert normalization["algorithm"] == "tauri-rendered-nsis-exact-path-map-v1"
    assert set(normalization) == {
        "algorithm",
        "raw_source_sha256",
        "structural_sha256",
        "normalized_sha256",
        "mapped_targets",
    }
    toolchain = cast(Mapping[str, object], created["toolchain"])
    trust = cast(Mapping[str, object], toolchain["trust"])
    assert trust["lock_sha256"] == _digest(contract.TOOLCHAIN_LOCK_PATH.read_bytes())
    assert trust["tree"] == contract._canonical_toolchain_tree(
        cast(list[Mapping[str, object]], created["files"])
    )
    normalized_script = (kit / "content/installer.nsi").read_text()
    assert "C:\\runner" not in normalized_script
    assert '!define MAIN "payload\\stock-desk.exe"' in normalized_script
    assert '!define HOOK "packaging\\installer-hooks.nsh"' in normalized_script
    assert {
        str(item["target"])
        for item in cast(list[Mapping[str, object]], normalization["mapped_targets"])
    } >= {"payload/stock-desk.exe", "packaging/installer-hooks.nsh"}


def test_independent_directories_produce_identical_kit_digest(tmp_path: Path) -> None:
    source_a, descriptor_a_value = _fixture(tmp_path / "runner-a", prefix="A")
    source_b, descriptor_b_value = _fixture(tmp_path / "runner-b", prefix="B")
    descriptor_a = _write_descriptor(tmp_path, descriptor_a_value, "a.json")
    descriptor_b = _write_descriptor(tmp_path, descriptor_b_value, "b.json")

    left = _create_kit(
        descriptor=descriptor_a, source_root=source_a, output=tmp_path / "left"
    )
    right = _create_kit(
        descriptor=descriptor_b, source_root=source_b, output=tmp_path / "right"
    )

    left_normalization = cast(Mapping[str, object], left["normalization"])
    right_normalization = cast(Mapping[str, object], right["normalization"])
    assert (
        left_normalization["raw_source_sha256"]
        != right_normalization["raw_source_sha256"]
    )
    assert (
        left_normalization["structural_sha256"]
        == right_normalization["structural_sha256"]
    )
    assert (
        left_normalization["normalized_sha256"]
        == right_normalization["normalized_sha256"]
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("argv", ["-V3", "installer.nsi"], "audited Tauri"),
        ("cleared_environment", True, "array"),
        ("source_sha", "main", "Git object"),
        ("source_epoch", 0, "positive"),
    ],
)
def test_descriptor_rejects_contract_drift(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    source, descriptor = _fixture(tmp_path)
    descriptor[field] = value

    with pytest.raises(contract.NsisRepackContractError, match=match):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tauri_cli_version", "2.11.5"),
        ("nsis_version", "3.12"),
        ("nsis_tauri_utils_version", "0.5.4"),
    ],
)
def test_toolchain_versions_are_hard_pinned(
    tmp_path: Path, field: str, value: str
) -> None:
    source, descriptor = _fixture(tmp_path)
    descriptor["toolchain"][field] = value

    with pytest.raises(contract.NsisRepackContractError, match=field):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


def test_strict_json_rejects_duplicate_and_unknown_fields(tmp_path: Path) -> None:
    source, descriptor = _fixture(tmp_path)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(contract.NsisRepackContractError, match="duplicate"):
        _create_kit(
            descriptor=duplicate, source_root=source, output=tmp_path / "duplicate-kit"
        )

    descriptor["surprise"] = True
    with pytest.raises(contract.NsisRepackContractError, match="unknown surprise"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "unknown-kit",
        )


@pytest.mark.parametrize("bad_path", ["../escape", "/absolute", "A\\B", "C:drive"])
def test_paths_are_normalized_relative_posix_paths(
    tmp_path: Path, bad_path: str
) -> None:
    source, descriptor = _fixture(tmp_path)
    descriptor["files"][0]["path"] = bad_path
    with pytest.raises(contract.NsisRepackContractError, match="POSIX"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


def test_case_collisions_fail_closed(tmp_path: Path) -> None:
    source, descriptor = _fixture(tmp_path)
    duplicate = copy.deepcopy(descriptor["files"][0])
    duplicate["path"] = str(duplicate["path"]).swapcase()
    descriptor["files"].append(duplicate)

    with pytest.raises(contract.NsisRepackContractError, match="collision"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


def test_source_root_symlink_is_not_resolved_away(tmp_path: Path) -> None:
    source, descriptor = _fixture(tmp_path)
    source_link = tmp_path / "source-link"
    source_link.symlink_to(source, target_is_directory=True)

    with pytest.raises(contract.NsisRepackContractError):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source_link,
            output=tmp_path / "kit",
        )


def test_mapping_must_be_exact_and_target_a_bound_artifact(tmp_path: Path) -> None:
    source, descriptor = _fixture(tmp_path)
    descriptor["path_mappings"][0]["occurrences"] = 2
    with pytest.raises(contract.NsisRepackContractError, match="occurrence"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "count-kit",
        )

    source, descriptor = _fixture(tmp_path / "second")
    descriptor["path_mappings"][0]["target"] = "unbound/app.exe"
    with pytest.raises(contract.NsisRepackContractError, match="bound payload"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path / "second", descriptor),
            source_root=source,
            output=tmp_path / "target-kit",
        )


def test_unmapped_absolute_and_unknown_plugin_calls_fail_closed(tmp_path: Path) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    payload = script.read_bytes() + b'File "D:\\unknown\\evil.exe"\n'
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)
    with pytest.raises(contract.NsisRepackContractError, match="unmapped absolute"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "absolute-kit",
        )

    source, descriptor = _fixture(tmp_path / "plugin")
    script = source / "installer.nsi"
    payload = script.read_bytes() + b"Mystery::Run\n"
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)
    with pytest.raises(contract.NsisRepackContractError, match="unknown plugin"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path / "plugin", descriptor),
            source_root=source,
            output=tmp_path / "plugin-kit",
        )


@pytest.mark.parametrize("attribute_option", ["/a", "/A"])
def test_file_cannot_preserve_unbound_source_attributes(
    tmp_path: Path, attribute_option: str
) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    absolute_payload = descriptor["path_mappings"][0]["source_absolute"]
    payload = (
        script.read_bytes()
        + (f'File {attribute_option} "/oname=copy.exe" "{absolute_payload}"\n').encode()
    )
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)
    descriptor["path_mappings"][0]["occurrences"] = 2

    with pytest.raises(
        contract.NsisRepackContractError, match="preserve source attributes"
    ):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


@pytest.mark.parametrize(
    "dynamic_script",
    [
        '!define COPY "File /a"\n${COPY} "/oname=copy.exe" "{payload}"\n',
        '!define COPY File\n${COPY} /a "/oname=copy.exe" "{payload}"\n',
    ],
)
def test_dynamic_file_instruction_cannot_bypass_attribute_audit(
    tmp_path: Path, dynamic_script: str
) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    absolute_payload = descriptor["path_mappings"][0]["source_absolute"]
    payload = (
        script.read_bytes()
        + dynamic_script.replace("{payload}", absolute_payload).encode()
    )
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)
    descriptor["path_mappings"][0]["occurrences"] = 2

    with pytest.raises(
        contract.NsisRepackContractError, match="dynamic preprocessor instruction"
    ):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


@pytest.mark.parametrize(
    "instruction",
    [
        "!execute cmd",
        "!makensis installer.nsi",
        "!packhdr temp.exe packer.exe",
        "!finalize cmd",
        "!uninstfinalize cmd",
        "!system 'whoami'",
        "nsExec::Exec cmd",
    ],
)
def test_build_time_external_control_instructions_are_forbidden(
    tmp_path: Path, instruction: str
) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    payload = script.read_bytes() + f"{instruction}\n".encode()
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)
    with pytest.raises(contract.NsisRepackContractError, match="forbidden"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


@pytest.mark.parametrize(
    "dynamic_script",
    [
        '!define CONTROL execute\n!${CONTROL} "cmd.exe /c whoami"\n',
        "!define CONTROL \"!system 'whoami'\"\n${CONTROL}\n",
        '!define CONTROL uninstfinalize\n!${CONTROL} "sign.cmd"\n',
        '!define BANG "!"\n!define CONTROL system\n${BANG}${CONTROL} "whoami"\n',
        '!define ALIAS CONTROL\n!define CONTROL execute\n!${${ALIAS}} "cmd"\n',
        "!define /ifndef CONTROL \"!system 'whoami'\"\n${CONTROL}\n",
        '!define /ifndef CONTROL execute\n!${CONTROL} "cmd.exe /c whoami"\n',
    ],
)
def test_dynamic_preprocessor_instruction_names_cannot_bypass_external_control_audit(
    tmp_path: Path, dynamic_script: str
) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    payload = script.read_bytes() + dynamic_script.encode()
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)

    with pytest.raises(contract.NsisRepackContractError, match="dynamic preprocessor"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


def test_nsis_script_cannot_read_an_undeclared_process_environment_variable(
    tmp_path: Path,
) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    payload = script.read_bytes() + b'DetailPrint "$%UNDECLARED_BUILD_SECRET%"\n'
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)

    with pytest.raises(
        contract.NsisRepackContractError, match="undeclared environment"
    ):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


def test_only_the_unique_empty_tauri_uninstaller_finalize_branch_is_allowed(
    tmp_path: Path,
) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    payload = script.read_bytes() + (
        b'!define UNINSTALLERSIGNCOMMAND ""\n'
        b'!if "${UNINSTALLERSIGNCOMMAND}" != ""\n'
        b"!uninstfinalize '${UNINSTALLERSIGNCOMMAND}'\n"
        b"!endif\n"
    )
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)

    created = _create_kit(
        descriptor=_write_descriptor(tmp_path, descriptor),
        source_root=source,
        output=tmp_path / "kit",
    )

    assert created["artifact"] == contract.KIT_ARTIFACT


@pytest.mark.parametrize(
    "branch",
    [
        (
            '!define UNINSTALLERSIGNCOMMAND "sign.cmd"\n'
            '!if "${UNINSTALLERSIGNCOMMAND}" != ""\n'
            "!uninstfinalize '${UNINSTALLERSIGNCOMMAND}'\n"
            "!endif\n"
        ),
        (
            '!define UNINSTALLERSIGNCOMMAND ""\n'
            '!if "${UNINSTALLERSIGNCOMMAND}" == ""\n'
            "!uninstfinalize '${UNINSTALLERSIGNCOMMAND}'\n"
            "!endif\n"
        ),
        (
            '!define UNINSTALLERSIGNCOMMAND ""\n'
            '!define UNINSTALLERSIGNCOMMAND ""\n'
            '!if "${UNINSTALLERSIGNCOMMAND}" != ""\n'
            "!uninstfinalize '${UNINSTALLERSIGNCOMMAND}'\n"
            "!endif\n"
        ),
    ],
)
def test_uninstaller_finalize_branch_must_be_provably_dead(
    tmp_path: Path, branch: str
) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    payload = script.read_bytes() + branch.encode()
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)
    with pytest.raises(contract.NsisRepackContractError, match="uninstaller|signing"):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


def test_verify_rejects_tampering_extra_files_and_wrong_source(tmp_path: Path) -> None:
    kit, _result = _create(tmp_path)
    payload = kit / "content/payload/stock-desk.exe"
    payload.chmod(0o600)
    payload.write_bytes(b"tampered")
    with pytest.raises(contract.NsisRepackContractError, match="identity mismatch"):
        _verify_kit(kit=kit)

    kit, _result = _create(tmp_path / "extra")
    content = kit / "content"
    content.chmod(0o700)
    (content / "extra.txt").write_text("extra")
    with pytest.raises(contract.NsisRepackContractError, match="unbound files"):
        _verify_kit(kit=kit)

    kit, _result = _create(tmp_path / "source")
    with pytest.raises(contract.NsisRepackContractError, match="source_sha"):
        _verify_kit(kit=kit, expected_source_sha="c" * 40)


def test_repack_uses_only_fixed_argv_and_environment_and_writes_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit, manifest = _create(tmp_path)
    observed: dict[str, object] = {}
    monkeypatch.setenv("NSISDIR", "unsafe")
    monkeypatch.setenv("NSISCONFDIR", "unsafe")
    monkeypatch.setenv("UNRELATED_INHERITED", "preserved")

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = command
        observed.update(kwargs)
        cwd = Path(str(kwargs["cwd"]))
        generated = cwd / "unsigned/stock-desk.exe"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(INSTALLER)
        return subprocess.CompletedProcess(command, 0, stdout=b"ok")

    monkeypatch.setattr("scripts.nsis_repack_contract.subprocess.run", fake_run)
    output = tmp_path / "release/stock-desk.exe"
    receipt_path = tmp_path / "release/repack.json"

    receipt = _repack(kit, output, receipt_path)

    command = cast(list[str], observed["command"])
    assert command[1:] == manifest["argv"]
    assert isinstance(observed["stdout"], int)
    assert observed["stderr"] is subprocess.STDOUT
    execution_environment = cast(dict[str, str], observed["env"])
    assert execution_environment["SOURCE_DATE_EPOCH"] == str(EPOCH)
    assert execution_environment["TZ"] == "UTC"
    assert "UNRELATED_INHERITED" not in execution_environment
    assert "NSISDIR" not in execution_environment
    assert "NSISCONFDIR" not in execution_environment
    assert set(execution_environment) == set(
        cast(Mapping[str, object], manifest["environment"])
    )
    assert execution_environment["TEMP"] == execution_environment["TMP"]
    assert Path(execution_environment["TEMP"]).name == ".private-temp"
    assert output.read_bytes() == INSTALLER
    assert receipt["artifact"] == contract.RECEIPT_ARTIFACT
    assert receipt["kit_sha256"] == manifest["kit_sha256"]
    receipt_output = cast(Mapping[str, object], receipt["output"])
    assert receipt_output["path"] == "stock-desk.exe"
    assert json.loads(receipt_path.read_bytes()) == receipt


def test_repack_removes_its_installer_when_final_output_snapshot_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit, _manifest = _create(tmp_path)
    monkeypatch.setattr(
        "scripts.nsis_repack_contract.subprocess.run", _fake_successful_makensis
    )
    original_snapshot = contract.snapshot_artifacts

    def reject_final_output_snapshot(
        source_root: Path,
        entries: list[str],
        destination: Path,
        *,
        limits: contract.SnapshotLimits,
    ) -> contract.SnapshotResult:
        if entries == ["out.exe"]:
            raise contract.SecureArtifactSnapshotError("forced final-output failure")
        return original_snapshot(source_root, entries, destination, limits=limits)

    monkeypatch.setattr(contract, "snapshot_artifacts", reject_final_output_snapshot)
    output = tmp_path / "out.exe"
    receipt = tmp_path / "receipt.json"

    with pytest.raises(contract.NsisRepackContractError, match="could not be secured"):
        _repack(kit, output, receipt)

    assert not output.exists()
    assert not receipt.exists()


def test_repack_receipt_remains_complete_when_path_writer_would_short_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit, _manifest = _create(tmp_path)
    monkeypatch.setattr(
        "scripts.nsis_repack_contract.subprocess.run", _fake_successful_makensis
    )
    output = tmp_path / "out.exe"
    receipt = tmp_path / "receipt.json"
    original_open = Path.open

    class ShortReceiptWriter:
        def __init__(self) -> None:
            self.stream: Any = None

        def __enter__(self) -> "ShortReceiptWriter":
            self.stream = original_open(receipt, "xb")
            return self

        def write(self, payload: bytes) -> int:
            assert self.stream is not None
            return self.stream.write(payload[:1])

        def flush(self) -> None:
            assert self.stream is not None
            self.stream.flush()

        def fileno(self) -> int:
            assert self.stream is not None
            return int(self.stream.fileno())

        def __exit__(self, *args: object) -> None:
            assert self.stream is not None
            self.stream.close()

    def short_receipt_open(path: Path, *args: object, **kwargs: object) -> Any:
        if path == receipt and args and args[0] == "xb":
            return ShortReceiptWriter()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", short_receipt_open)

    generated = _repack(kit, output, receipt)

    assert json.loads(receipt.read_bytes()) == generated


def test_exclusive_writer_completes_short_os_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write

    def short_write(descriptor: int, payload: bytes | memoryview) -> int:
        return original_write(descriptor, bytes(payload[:1]))

    monkeypatch.setattr(contract.os, "write", short_write)
    destination = tmp_path / "receipt.json"
    payload = b'{"complete":true}\n'

    identity = contract._write_new_file(destination, payload, "receipt")

    assert destination.read_bytes() == payload
    metadata = destination.stat(follow_symlinks=False)
    assert identity == (metadata.st_dev, metadata.st_ino)


def test_schema_files_close_every_object_shape() -> None:
    for name in (
        "nsis-repack-kit-v1.schema.json",
        "nsis-repack-receipt-v1.schema.json",
    ):
        schema = json.loads((Path("schemas") / name).read_bytes())
        stack: list[object] = [schema]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                if current.get("type") == "object":
                    assert current.get("additionalProperties") is False
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)


def test_cli_create_verify_and_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, descriptor = _fixture(tmp_path)
    descriptor_path = _write_descriptor(tmp_path, descriptor)
    kit = tmp_path / "kit"
    assert (
        contract.main(
            [
                "create-kit",
                "--descriptor",
                str(descriptor_path),
                "--source-root",
                str(source),
                "--output",
                str(kit),
                "--expected-source-sha",
                SHA,
                "--expected-source-tree",
                TREE,
            ]
        )
        == 0
    )
    manifest = json.loads((kit / contract.KIT_MANIFEST).read_bytes())
    verification_arguments = [
        "--expected-source-sha",
        SHA,
        "--expected-source-tree",
        TREE,
        "--expected-kit-sha256",
        str(manifest["kit_sha256"]),
    ]
    assert (
        contract.main(["verify-kit", "--kit", str(kit), *verification_arguments]) == 0
    )
    assert (
        contract.main(
            [
                "verify-kit",
                "--kit",
                str(tmp_path / "missing"),
                *verification_arguments,
            ]
        )
        == 1
    )
    assert "failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "entrypoint",
    [
        (os.fspath(Path("scripts") / "nsis_repack_contract.py"),),
        ("-m", "scripts.nsis_repack_contract"),
    ],
)
def test_cli_can_run_from_repository_root(entrypoint: tuple[str, ...]) -> None:
    result = subprocess.run(
        [sys.executable, *entrypoint, "--help"],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda: contract._object([], "value"), "object"),
        (lambda: contract._array("text", "value"), "array"),
        (lambda: contract._text("", "value"), "invalid"),
        (lambda: contract._text("bad\x00", "value"), "invalid"),
        (lambda: contract._digest("A" * 64, "digest"), "lowercase"),
        (lambda: contract._positive_int(True, "count"), "positive"),
        (lambda: contract._positive_int(-1, "count", allow_zero=True), "non-negative"),
        (lambda: contract._relative_path(".", "path"), "POSIX"),
        (lambda: contract._relative_path("a/../b", "path"), "POSIX"),
        (lambda: contract._mapping_target("../toolchain", "target"), "traverse"),
        (lambda: contract._mapping_target("/toolchain", "target"), "portable"),
        (lambda: contract._mapping_target("C:/toolchain", "target"), "portable"),
        (
            lambda: contract._mapping_target("toolchain\\Include", "target"),
            "portable",
        ),
    ],
)
def test_primitive_contract_validators_fail_closed(call: Any, match: str) -> None:
    with pytest.raises(contract.NsisRepackContractError, match=match):
        call()


@pytest.mark.parametrize("role", ["icon", "nsis-hook", "payload", "webview2"])
def test_file_roles_used_only_by_rendered_script_are_not_shape_requirements(
    tmp_path: Path, role: str
) -> None:
    _source, descriptor = _fixture(tmp_path)
    files = [record for record in descriptor["files"] if record["role"] != role]

    normalized = contract._normalize_file_records(files)

    assert role not in {record["role"] for record in normalized}


def test_descriptor_normalizes_bound_toolchain_directory_mapping(
    tmp_path: Path,
) -> None:
    source, descriptor = _fixture(tmp_path)
    absolute_toolchain = "C:\\runner\\A"
    script = source / "installer.nsi"
    rendered = (
        script.read_bytes()
        + (f'!define STOCK_DESK_NSIS_ROOT "{absolute_toolchain}"\n').encode()
    )
    script.write_bytes(rendered)
    script_record = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    script_record["size"] = len(rendered)
    script_record["sha256"] = _digest(rendered)
    descriptor["path_mappings"].append(
        {
            "source_absolute": absolute_toolchain,
            "target": "toolchain/.",
            "occurrences": 1,
        }
    )

    manifest = _create_kit(
        descriptor=_write_descriptor(tmp_path, descriptor),
        source_root=source,
        output=tmp_path / "kit",
    )

    assert {item["target"] for item in manifest["normalization"]["mapped_targets"]} >= {
        "toolchain"
    }


@pytest.mark.parametrize(
    "case",
    [
        "unknown-role",
        "oversized-file",
        "non-boolean-executable",
        "payload-executable",
        "missing-role",
        "second-executable",
        "second-rendered",
        "tool-digest",
        "missing-tool-file",
        "empty-plugins",
        "invalid-plugin-name",
        "plugin-digest",
        "unlisted-plugin-file",
        "wrong-utils-path",
        "empty-argv",
        "unknown-environment",
        "wrong-epoch-environment",
        "zero-output",
        "empty-mappings",
        "relative-mapping-source",
        "prefix-mapping-targets",
    ],
)
def test_descriptor_security_mutations_are_rejected(tmp_path: Path, case: str) -> None:
    source, descriptor = _fixture(tmp_path)
    files = descriptor["files"]
    tool = next(
        record for record in files if record["path"] == "toolchain/makensis.exe"
    )
    payload = next(record for record in files if record["role"] == "payload")
    if case == "unknown-role":
        payload["role"] = "mystery"
    elif case == "oversized-file":
        payload["size"] = contract.MAX_FILE_BYTES + 1
    elif case == "non-boolean-executable":
        payload["executable"] = 1
    elif case == "payload-executable":
        payload["executable"] = True
    elif case == "missing-role":
        descriptor["files"] = [
            record for record in files if record["role"] != "nsis-language"
        ]
    elif case == "second-executable":
        second = next(
            record
            for record in files
            if record["role"] == "nsis-toolchain" and record is not tool
        )
        second["executable"] = True
    elif case == "second-rendered":
        duplicate = copy.deepcopy(
            next(record for record in files if record["role"] == "nsis-rendered-script")
        )
        duplicate["path"] = "second.nsi"
        files.append(duplicate)
    elif case == "tool-digest":
        descriptor["toolchain"]["sha256"] = "f" * 64
    elif case == "missing-tool-file":
        descriptor["files"] = [
            record
            for record in files
            if record["path"] != "toolchain/Include/nsDialogs.nsh"
        ]
    elif case == "empty-plugins":
        descriptor["toolchain"]["plugins"] = []
    elif case == "invalid-plugin-name":
        descriptor["toolchain"]["plugins"][0]["name"] = "bad plugin"
    elif case == "plugin-digest":
        descriptor["toolchain"]["plugins"][0]["sha256"] = "f" * 64
    elif case == "unlisted-plugin-file":
        duplicate = copy.deepcopy(
            next(record for record in files if record["role"] == "nsis-plugin")
        )
        duplicate["path"] = "toolchain/Plugins/extra.dll"
        files.append(duplicate)
    elif case == "wrong-utils-path":
        next(record for record in files if record["path"] == "utils.nsh")["path"] = (
            "other-utils.nsh"
        )
    elif case == "empty-argv":
        descriptor["argv"] = []
    elif case == "unknown-environment":
        descriptor["environment"]["PATH"] = "unsafe"
    elif case == "wrong-epoch-environment":
        descriptor["environment"]["SOURCE_DATE_EPOCH"] = "1"
    elif case == "zero-output":
        descriptor["expected_unsigned_installer"]["size"] = 0
    elif case == "empty-mappings":
        descriptor["path_mappings"] = []
    elif case == "relative-mapping-source":
        descriptor["path_mappings"][0]["source_absolute"] = "relative.exe"
    else:
        duplicate = copy.deepcopy(descriptor["path_mappings"][0])
        duplicate["source_absolute"] += "-second"
        duplicate["target"] += "/nested"
        descriptor["path_mappings"].append(duplicate)

    with pytest.raises(contract.NsisRepackContractError):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


@pytest.mark.parametrize(
    ("line", "match"),
    [
        ('File "payload/*.exe"', "dynamic File"),
        ('File "payload/missing.exe"', "not in the kit"),
        ('File "/tmp/evil.exe"', "normalized POSIX"),
        ('File "${UNKNOWN}"', "unbound path definition"),
        ('OutFile "/tmp/evil.exe"', "normalized POSIX"),
        ('OutFile "payload/other.exe"', "expected output"),
        ('OutFile "payload/a.exe" "payload/b.exe"', "unknown OutFile"),
        ('!include "missing.nsh"', "include is not in the kit"),
        ('!include "utils.nsh" "extra"', "unknown include"),
        ('!addplugindir "payload"', "plugin directory is not bound"),
        ('!addplugindir "payload" "extra"', "unknown plugin directory"),
    ],
)
def test_script_source_and_control_boundaries(
    tmp_path: Path, line: str, match: str
) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    payload = script.read_bytes() + f"{line}\n".encode()
    script.write_bytes(payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(payload)
    rendered["sha256"] = _digest(payload)
    with pytest.raises(contract.NsisRepackContractError, match=match):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


def test_bare_include_requires_one_unambiguous_bound_suffix(tmp_path: Path) -> None:
    source, descriptor = _fixture(tmp_path)
    script = source / "installer.nsi"
    rendered_payload = script.read_bytes() + b'!include "MUI2.nsh"\n'
    script.write_bytes(rendered_payload)
    rendered = next(
        record for record in descriptor["files"] if record["path"] == "installer.nsi"
    )
    rendered["size"] = len(rendered_payload)
    rendered["sha256"] = _digest(rendered_payload)
    duplicate_path = "other/MUI2.nsh"
    duplicate_payload = b"other MUI\n"
    duplicate = source / duplicate_path
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(duplicate_payload)
    descriptor["files"].append(
        {
            "path": duplicate_path,
            "role": "nsis-include",
            "size": len(duplicate_payload),
            "sha256": _digest(duplicate_payload),
            "executable": False,
        }
    )

    with pytest.raises(
        contract.NsisRepackContractError, match="include is not in the kit"
    ):
        _create_kit(
            descriptor=_write_descriptor(tmp_path, descriptor),
            source_root=source,
            output=tmp_path / "kit",
        )


@pytest.mark.parametrize("mode", ["raises", "nonzero", "wrong-output"])
def test_repack_execution_failures_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    kit, _manifest = _create(tmp_path)

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if mode == "raises":
            raise OSError("cannot execute")
        cwd = Path(str(kwargs["cwd"]))
        generated = cwd / "unsigned/stock-desk.exe"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(b"wrong\n" if mode == "wrong-output" else INSTALLER)
        return subprocess.CompletedProcess(command, 7 if mode == "nonzero" else 0)

    monkeypatch.setattr("scripts.nsis_repack_contract.subprocess.run", fake_run)
    with pytest.raises(contract.NsisRepackContractError) as captured:
        _repack(kit, tmp_path / "out.exe", tmp_path / "receipt.json")
    if mode == "wrong-output":
        message = str(captured.value)
        assert f"expected size={len(INSTALLER)} sha256={_digest(INSTALLER)}" in message
        assert f"actual size={len(b'wrong\n')} sha256={_digest(b'wrong\n')}" in message


def test_repack_nonzero_reports_only_a_bounded_redacted_tool_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit, _manifest = _create(tmp_path)
    observed_work: Path | None = None

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal observed_work
        observed_work = Path(str(kwargs["cwd"]))
        output_fd = cast(int, kwargs["stdout"])
        spaced_drive_path = "".join(
            ("C:", "/", "Users", "/", "Runner Name", "/private/file.nsi")
        )
        forward_unc_path = "".join(("/", "/", "SERVER", "/share/private/file.nsi"))
        os.write(output_fd, b"discarded-prefix" + b"x" * 65536 + b"\n")
        os.write(
            output_fd,
            (
                f"Error in script {observed_work}\\installer.nsi on line 576"
                "\u009b\u202e\n"
                f"Secondary {spaced_drive_path}\n"
                f"UNC {forward_unc_path}\n"
            ).encode(),
        )
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr("scripts.nsis_repack_contract.subprocess.run", fake_run)

    with pytest.raises(contract.NsisRepackContractError) as captured:
        _repack(kit, tmp_path / "out.exe", tmp_path / "receipt.json")

    message = str(captured.value)
    assert "NSIS toolchain returned 7" in message
    assert "NSIS> Error in script @STOCK_DESK_PRIVATE_WORK@" in message
    assert observed_work is not None
    assert str(observed_work) not in message
    assert "discarded-prefix" not in message
    assert "@ABSOLUTE_PATH@" in message
    assert "Runner Name" not in message
    assert "SERVER" not in message
    assert "\u009b" not in message
    assert "\u202e" not in message
    assert len(message.encode()) <= contract.NSIS_DIAGNOSTIC_TAIL_BYTES + 1024


def test_repack_closes_both_pipe_fds_when_diagnostic_reader_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit, _manifest = _create(tmp_path)
    real_pipe = os.pipe
    opened_fds: list[int] = []

    def observed_pipe() -> tuple[int, int]:
        pair = real_pipe()
        opened_fds.extend(pair)
        return pair

    class FailingThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("forced start failure")

    monkeypatch.setattr(contract.os, "pipe", observed_pipe)
    monkeypatch.setattr(contract.threading, "Thread", FailingThread)

    with pytest.raises(contract.NsisRepackContractError, match="could not start"):
        _repack(kit, tmp_path / "out.exe", tmp_path / "receipt.json")

    assert len(opened_fds) == 2
    for file_descriptor in opened_fds:
        with pytest.raises(OSError):
            os.fstat(file_descriptor)


def test_repack_rejects_existing_destinations(tmp_path: Path) -> None:
    kit, _manifest = _create(tmp_path)
    output = tmp_path / "out.exe"
    output.write_bytes(b"existing")
    with pytest.raises(
        contract.NsisRepackContractError, match="must not already exist"
    ):
        _repack(kit, output, tmp_path / "receipt.json")

    output.unlink()
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b"existing")
    with pytest.raises(
        contract.NsisRepackContractError, match="must not already exist"
    ):
        _repack(kit, output, receipt)


@pytest.mark.parametrize("path", ["CON", "aux.txt", "folder/trailing. ", "file."])
def test_windows_nonportable_paths_are_rejected(path: str) -> None:
    with pytest.raises(contract.NsisRepackContractError, match="portable"):
        contract._relative_path(path, "path")


def test_repack_executes_the_same_private_snapshot_that_was_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit, _manifest = _create(tmp_path)
    original_context = contract._verified_kit_snapshot

    @contextmanager
    def mutate_original_after_snapshot(
        *,
        kit: Path,
        expected_source_sha: str | None = None,
        expected_source_tree: str | None = None,
        expected_kit_sha256: str | None = None,
    ) -> Any:
        with original_context(
            kit=kit,
            expected_source_sha=expected_source_sha,
            expected_source_tree=expected_source_tree,
            expected_kit_sha256=expected_kit_sha256,
        ) as verified:
            original_payload = kit / "content/payload/stock-desk.exe"
            original_payload.chmod(0o600)
            original_payload.write_bytes(b"mutated-after-verification")
            yield verified

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        cwd = Path(str(kwargs["cwd"]))
        assert (cwd / "payload/stock-desk.exe").read_bytes() == b"application\n"
        generated = cwd / "unsigned/stock-desk.exe"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(INSTALLER)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(
        contract, "_verified_kit_snapshot", mutate_original_after_snapshot
    )
    monkeypatch.setattr("scripts.nsis_repack_contract.subprocess.run", fake_run)
    result = _repack(kit, tmp_path / "out.exe", tmp_path / "receipt.json")
    assert result["artifact"] == contract.RECEIPT_ARTIFACT


def _fake_successful_makensis(
    command: list[str], **kwargs: object
) -> subprocess.CompletedProcess[bytes]:
    cwd = Path(str(kwargs["cwd"]))
    generated = cwd / "unsigned/stock-desk.exe"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_bytes(INSTALLER)
    return subprocess.CompletedProcess(command, 0)


def test_verify_receipt_closes_kit_and_installer_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit, _manifest = _create(tmp_path)
    monkeypatch.setattr(
        "scripts.nsis_repack_contract.subprocess.run", _fake_successful_makensis
    )
    output = tmp_path / "out.exe"
    receipt = tmp_path / "receipt.json"
    generated = _repack(kit, output, receipt)

    verified = _verify_receipt(
        receipt=receipt,
        kit=kit,
        output=output,
        expected_source_sha=SHA,
        expected_source_tree=TREE,
    )

    assert verified == generated
    assert (
        contract.main(
            [
                "verify-receipt",
                "--receipt",
                str(receipt),
                "--kit",
                str(kit),
                "--output",
                str(output),
                "--expected-source-sha",
                SHA,
                "--expected-source-tree",
                TREE,
                "--expected-kit-sha256",
                str(generated["kit_sha256"]),
            ]
        )
        == 0
    )
    output.chmod(0o600)
    output.write_bytes(b"tampered")
    with pytest.raises(contract.NsisRepackContractError, match="installer bytes"):
        _verify_receipt(receipt=receipt, kit=kit, output=output)


def test_verify_receipt_rejects_noncanonical_or_mismatched_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit, _manifest = _create(tmp_path)
    monkeypatch.setattr(
        "scripts.nsis_repack_contract.subprocess.run", _fake_successful_makensis
    )
    output = tmp_path / "out.exe"
    receipt = tmp_path / "receipt.json"
    _repack(kit, output, receipt)
    value = json.loads(receipt.read_bytes())
    receipt.chmod(0o600)
    receipt.write_text(json.dumps(value, indent=2), encoding="utf-8")
    with pytest.raises(contract.NsisRepackContractError, match="canonical JSON"):
        _verify_receipt(receipt=receipt, kit=kit, output=output)

    value["source_sha"] = "c" * 40
    value.pop("receipt_sha256")
    value["receipt_sha256"] = contract._receipt_digest(value)
    receipt.write_bytes(contract._canonical_json(value))
    with pytest.raises(contract.NsisRepackContractError, match="verified kit"):
        _verify_receipt(receipt=receipt, kit=kit, output=output)


def test_verify_receipt_rejects_output_size_above_contract_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit, _manifest = _create(tmp_path)
    monkeypatch.setattr(
        "scripts.nsis_repack_contract.subprocess.run", _fake_successful_makensis
    )
    output = tmp_path / "out.exe"
    receipt = tmp_path / "receipt.json"
    _repack(kit, output, receipt)
    value = json.loads(receipt.read_bytes())
    value["output"]["size"] = contract.MAX_FILE_BYTES + 1
    value.pop("receipt_sha256")
    value["receipt_sha256"] = contract._receipt_digest(value)
    receipt.chmod(0o600)
    receipt.write_bytes(contract._canonical_json(value))

    with pytest.raises(contract.NsisRepackContractError, match="exceeds size limit"):
        _verify_receipt(receipt=receipt, kit=kit, output=output)
