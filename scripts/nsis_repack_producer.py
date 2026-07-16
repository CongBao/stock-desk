"""Fail-closed producer for the reviewed Windows x64 NSIS repack evidence."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tomllib
from typing import Final, Literal

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).absolute().parent.parent))

from scripts.nsis_repack_contract import (
    patch_tauri_bundle_payload,
    verify_extracted_nsis_toolchain,
)
from scripts.secure_artifact_snapshot import (
    private_directory_lease,
    prepare_private_directory,
    SecureArtifactSnapshotError,
    SnapshotLimits,
    snapshot_artifacts,
    verify_no_windows_named_streams,
)


class NsisRepackProducerError(ValueError):
    """The live Windows NSIS build does not match the reviewed producer contract."""


@dataclass(frozen=True)
class ProducerSource:
    event_name: Literal["push", "pull_request"]
    source_ref: str
    source_sha: str
    source_tree: str
    source_epoch: int
    github_sha: str


@dataclass(frozen=True)
class ProducerAnchors:
    repository: Path
    base_config: Path
    windows_config: Path
    cargo_toml: Path
    nsis_template: Path
    render_root: Path
    release_root: Path
    bundle_root: Path
    tauri_tools_root: Path
    nsis_root: Path


@dataclass(frozen=True)
class StageFileSpec:
    snapshot_source: Path
    target: str
    role: str
    executable: bool = False


@dataclass(frozen=True)
class PathUse:
    purpose: str
    source_absolute: str
    target: str
    occurrences: int


@dataclass(frozen=True)
class ProducerResult:
    stage: Path
    descriptor: Path
    producer_receipt: Path
    original_candidate: Path


@dataclass(frozen=True)
class EffectiveNsisConfig:
    template: Path
    hook: Path
    icon: Path
    languages: tuple[str, str]
    language_sources: tuple[Path, Path]
    sidecar: Path


@dataclass(frozen=True)
class RenderedNsisContract:
    path_uses: tuple[PathUse, ...]
    path_mappings: tuple[PathUse, ...]
    webview_relative: str
    plugin_names: tuple[str, ...]


_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_PR_REF: Final = re.compile(r"^refs/pull/[1-9][0-9]*/merge$")
_DEFINE: Final = re.compile(
    r'^\s*!define\s+([A-Za-z][A-Za-z0-9_]*)\s+"([^"\r\n]*)"\s*$', re.MULTILINE
)
_INCLUDE: Final = re.compile(r'^\s*!include\s+(?:"([^"\r\n]+)"|([^\s;"\r\n]+))\s*$')
_PLUGIN_CALL: Final = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.-]*)::", re.MULTILINE)
_QUOTED_WINDOWS_ABSOLUTE: Final = re.compile(r'"((?:[A-Za-z]:[\\/]|\\\\)[^"\r\n]+)"')
_SAFE_CACHE_COMPONENT: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXPECTED_PLUGINS: Final = ("NSISdl", "System", "nsDialogs", "nsis_tauri_utils")
_EMPTY_DEFINES: Final = (
    "WEBVIEW2BOOTSTRAPPERPATH",
    "MINIMUMWEBVIEW2VERSION",
    "LICENSE",
    "SIDEBARIMAGE",
    "HEADERIMAGE",
    "UNINSTALLERHEADERIMAGE",
    "UNINSTALLERSIGNCOMMAND",
)
_RENDER_INVENTORY: Final = (
    "FileAssociation.nsh",
    "English.nsh",
    "SimpChinese.nsh",
    "installer.nsi",
    "utils.nsh",
)
_PLUGIN_PATHS: Final = {
    "NSISdl": "toolchain/Plugins/x86-unicode/NSISdl.dll",
    "System": "toolchain/Plugins/x86-unicode/System.dll",
    "nsDialogs": "toolchain/Plugins/x86-unicode/nsDialogs.dll",
    "nsis_tauri_utils": (
        "toolchain/Plugins/x86-unicode/additional/nsis_tauri_utils.dll"
    ),
}
_PRODUCER_RECEIPT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "artifact",
        "source",
        "repository_selection",
        "repository_snapshot",
        "tools_selection",
        "tools_snapshot",
        "render_inventory",
        "original_candidate",
        "restored_host",
        "webview_relative",
        "descriptor",
    }
)


def merge_rfc7396(base: object, patch: object) -> object:
    """Return an RFC 7396 JSON Merge Patch result without mutating either input."""

    if not isinstance(patch, Mapping):
        return deepcopy(patch)
    result: dict[str, object]
    if isinstance(base, Mapping):
        result = {str(key): deepcopy(value) for key, value in base.items()}
    else:
        result = {}
    for key, value in patch.items():
        if not isinstance(key, str):
            raise NsisRepackProducerError("JSON object keys must be strings")
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_rfc7396(result.get(key), value)
    return result


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NsisRepackProducerError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def parse_json_strict(payload: bytes, *, field: str) -> object:
    """Parse one UTF-8 JSON document while rejecting duplicate object members."""

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except NsisRepackProducerError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NsisRepackProducerError(f"{field} is not strict UTF-8 JSON") from error


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                size += len(block)
                digest.update(block)
    except OSError as error:
        raise NsisRepackProducerError(
            "selected producer file could not be hashed"
        ) from error
    return size, digest.hexdigest()


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise NsisRepackProducerError(f"{field} must be an object")
    return value


def _exact(value: object, expected: object, field: str) -> None:
    if value != expected or type(value) is not type(expected):
        raise NsisRepackProducerError(f"{field} must be exactly {expected!r}")


def validate_effective_config(
    base: Mapping[str, object],
    windows: Mapping[str, object],
    anchors: ProducerAnchors,
) -> EffectiveNsisConfig:
    """Validate the exact reviewed Tauri Windows x64 NSIS configuration."""

    effective = _mapping(merge_rfc7396(base, windows), "effective config")
    bundle = _mapping(effective.get("bundle"), "bundle")
    _exact(bundle.get("active"), True, "bundle.active")
    _exact(bundle.get("targets"), ["nsis"], "bundle.targets")
    if "resources" in bundle:
        raise NsisRepackProducerError("bundle.resources must be absent")
    _exact(
        bundle.get("externalBin"),
        ["binaries/stock-desk-sidecar"],
        "bundle.externalBin",
    )
    windows_config = _mapping(bundle.get("windows"), "bundle.windows")
    _exact(
        windows_config.get("webviewInstallMode"),
        {"type": "offlineInstaller"},
        "bundle.windows.webviewInstallMode offlineInstaller",
    )
    _exact(
        windows_config.get("allowDowngrades"),
        False,
        "bundle.windows.allowDowngrades",
    )
    for signing_field in ("certificateThumbprint", "signCommand"):
        if signing_field in windows_config:
            raise NsisRepackProducerError("Windows signing fields must be absent")
    nsis = _mapping(windows_config.get("nsis"), "bundle.windows.nsis")
    required = {
        "installMode": "currentUser",
        "template": "../packaging/nsis/installer.nsi",
        "installerHooks": "../packaging/nsis/installer-hooks.nsh",
        "installerIcon": "icons/icon.ico",
        "uninstallerIcon": "icons/icon.ico",
        "languages": ["English", "SimpChinese"],
        "customLanguageFiles": {
            "English": "../packaging/nsis/languages/English.nsh",
            "SimpChinese": "../packaging/nsis/languages/SimpChinese.nsh",
        },
        "displayLanguageSelector": True,
    }
    for field, expected in required.items():
        _exact(nsis.get(field), expected, f"bundle.windows.nsis.{field}")
    forbidden = {
        "license",
        "sidebarImage",
        "headerImage",
        "uninstallerHeaderImage",
        "signedPluginsPath",
    }
    if forbidden.intersection(nsis):
        raise NsisRepackProducerError(
            "NSIS signing or optional image fields must be absent"
        )
    return EffectiveNsisConfig(
        template=anchors.nsis_template,
        hook=anchors.repository / "packaging/nsis/installer-hooks.nsh",
        icon=anchors.repository / "src-tauri/icons/icon.ico",
        languages=("English", "SimpChinese"),
        language_sources=(
            anchors.repository / "packaging/nsis/languages/English.nsh",
            anchors.repository / "packaging/nsis/languages/SimpChinese.nsh",
        ),
        sidecar=anchors.repository
        / "src-tauri/binaries/stock-desk-sidecar-x86_64-pc-windows-msvc.exe",
    )


def _windows_text(path: Path) -> str:
    return os.fspath(path).replace("/", "\\")


def validate_windows_path_text(path_text: str, *, field: str) -> str:
    """Require one normalized local-drive Windows path with no aliases or ADS."""

    if (
        re.fullmatch(r"[A-Za-z]:\\[^\r\n]+", path_text) is None
        or "/" in path_text
        or path_text.startswith(("\\\\", "\\?\\", "\\.\\"))
        or ":" in path_text[2:]
    ):
        raise NsisRepackProducerError(f"{field} must be a normalized local-drive path")
    components = path_text[3:].split("\\")
    if (
        not components
        or any(component in {"", ".", ".."} for component in components)
        or any(
            component != component.strip() or component.endswith(".")
            for component in components
        )
    ):
        raise NsisRepackProducerError(f"{field} contains a noncanonical component")
    return path_text


def validate_render_inventory(
    render_root: Path, *, english_source: Path, chinese_source: Path
) -> tuple[str, ...]:
    """Validate the exact rendered directory and BOM-prefixed language copies."""

    try:
        entries = sorted(os.scandir(render_root), key=lambda entry: entry.name.encode())
    except (OSError, UnicodeError) as error:
        raise NsisRepackProducerError(
            "render inventory cannot be enumerated"
        ) from error
    names = tuple(entry.name for entry in entries)
    if names != tuple(sorted(_RENDER_INVENTORY, key=lambda name: name.encode())):
        raise NsisRepackProducerError(
            "render inventory must contain exactly five files"
        )
    for entry in entries:
        try:
            metadata = os.lstat(entry.path)
        except OSError as error:
            raise NsisRepackProducerError(
                "render inventory entry is unreadable"
            ) from error
        if (
            entry.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or int(getattr(metadata, "st_file_attributes", 0)) & 0x400
        ):
            raise NsisRepackProducerError(
                "render inventory contains a nonordinary entry"
            )
    for language, source in (
        ("English", english_source),
        ("SimpChinese", chinese_source),
    ):
        try:
            expected = b"\xef\xbb\xbf" + source.read_bytes()
            actual = (render_root / f"{language}.nsh").read_bytes()
        except OSError as error:
            raise NsisRepackProducerError(
                f"{language} language copy is unreadable"
            ) from error
        if actual != expected:
            raise NsisRepackProducerError(
                f"{language} render copy does not equal BOM plus tracked source"
            )
    return _RENDER_INVENTORY


def select_original_candidate(bundle_root: Path) -> Path:
    """Select exactly one direct ordinary EXE and reject every nested candidate."""

    try:
        entries = list(os.scandir(bundle_root))
    except OSError as error:
        raise NsisRepackProducerError(
            "bundle candidate root cannot be enumerated"
        ) from error
    candidates: list[Path] = []
    nested_candidate = False
    for entry in entries:
        path = Path(entry.path)
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise NsisRepackProducerError(
                "bundle candidate entry is unreadable"
            ) from error
        if (
            entry.is_symlink()
            or int(getattr(metadata, "st_file_attributes", 0)) & 0x400
        ):
            raise NsisRepackProducerError(
                "bundle candidate root contains an unsafe entry"
            )
        if stat.S_ISREG(metadata.st_mode):
            if path.suffix.lower() == ".exe":
                if metadata.st_nlink != 1 or path.suffix != ".exe":
                    raise NsisRepackProducerError("bundle candidate is not canonical")
                candidates.append(path)
            else:
                raise NsisRepackProducerError(
                    "bundle candidate root contains an extra file"
                )
        elif stat.S_ISDIR(metadata.st_mode):
            for child_root, directories, files in os.walk(path):
                if directories or any(name.lower().endswith(".exe") for name in files):
                    nested_candidate = True
                    break
        else:
            raise NsisRepackProducerError(
                "bundle candidate root contains an unsafe entry"
            )
    if nested_candidate or len(candidates) != 1:
        raise NsisRepackProducerError("bundle candidate must be one direct EXE")
    return candidates[0]


def _one_define(defines: Mapping[str, list[str]], name: str) -> str:
    values = defines.get(name, [])
    if len(values) != 1:
        raise NsisRepackProducerError(f"{name} must be defined exactly once")
    return values[0]


def _require_ordinal(actual: str, expected: str, field: str) -> None:
    if actual != expected:
        raise NsisRepackProducerError(
            f"{field} does not equal its exact authorized path"
        )


def _count_exact_line(text: str, line: str, field: str) -> None:
    count = sum(candidate.strip() == line for candidate in text.splitlines())
    if count != 1:
        raise NsisRepackProducerError(f"{field} must occur exactly once")


def _directive_lines(text: str, directive: str) -> tuple[str, ...]:
    pattern = re.compile(rf"^{re.escape(directive)}(?:\s|$)", re.IGNORECASE)
    return tuple(
        stripped
        for line in text.splitlines()
        if (stripped := line.strip()) and pattern.match(stripped)
    )


def _include_values(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for line in _directive_lines(text, "!include"):
        match = _INCLUDE.fullmatch(line)
        if match is None:
            raise NsisRepackProducerError("include directive is not canonical")
        values.append(match.group(1) or match.group(2))
    return tuple(values)


def analyze_rendered_installer(
    text: str,
    *,
    config: EffectiveNsisConfig,
    anchors: ProducerAnchors,
    cargo_package_name: str,
) -> RenderedNsisContract:
    """Parse and compare the rendered script to the exact role/purpose multiset."""

    if "\x00" in text or "\r" in text.replace("\r\n", ""):
        raise NsisRepackProducerError("rendered installer text is not canonical")
    defines: dict[str, list[str]] = {}
    for match in _DEFINE.finditer(text):
        defines.setdefault(match.group(1), []).append(match.group(2))

    if _one_define(defines, "MAINBINARYNAME") != cargo_package_name:
        raise NsisRepackProducerError("MAINBINARYNAME does not equal the Cargo package")
    main = _windows_text(anchors.release_root / f"{cargo_package_name}.exe")
    plugin_dir = _windows_text(anchors.nsis_root / "Plugins/x86-unicode/additional")
    _require_ordinal(
        _one_define(defines, "MAINBINARYSRCPATH"), main, "MAINBINARYSRCPATH"
    )
    _require_ordinal(
        _one_define(defines, "ADDITIONALPLUGINSPATH"),
        plugin_dir,
        "ADDITIONALPLUGINSPATH",
    )
    if _one_define(defines, "INSTALLWEBVIEW2MODE") != "offlineInstaller":
        raise NsisRepackProducerError("INSTALLWEBVIEW2MODE must be offlineInstaller")
    for name in _EMPTY_DEFINES:
        if _one_define(defines, name) != "":
            raise NsisRepackProducerError(f"{name} must be empty")
    if _one_define(defines, "OUTFILE") != "nsis-output.exe":
        raise NsisRepackProducerError("OUTFILE must be nsis-output.exe")
    _count_exact_line(text, 'OutFile "${OUTFILE}"', "OutFile")
    _count_exact_line(
        text,
        '!addplugindir "${ADDITIONALPLUGINSPATH}"',
        "additional plugin directory",
    )
    if _directive_lines(text, "!addplugindir") != (
        '!addplugindir "${ADDITIONALPLUGINSPATH}"',
    ):
        raise NsisRepackProducerError(
            "plugin directory directives do not equal the audited set"
        )
    _count_exact_line(text, 'File "${MAINBINARYSRCPATH}"', "main host File")
    _count_exact_line(
        text,
        'File "/oname=$TEMP\\MicrosoftEdgeWebView2RuntimeInstaller.exe" '
        '"${WEBVIEW2INSTALLERPATH}"',
        "offline WebView2 File",
    )

    webview = _one_define(defines, "WEBVIEW2INSTALLERPATH")
    webview_prefix = _windows_text(anchors.tauri_tools_root) + "\\"
    if not webview.startswith(webview_prefix):
        raise NsisRepackProducerError("WEBVIEW2INSTALLERPATH is outside the x64 cache")
    webview_relative = webview[len(webview_prefix) :].replace("\\", "/")
    parts = webview_relative.split("/")
    if (
        len(parts) != 3
        or parts[0] != "x64"
        or any(_SAFE_CACHE_COMPONENT.fullmatch(part) is None for part in parts[1:])
    ):
        raise NsisRepackProducerError("WEBVIEW2INSTALLERPATH is not a bounded x64 leaf")

    icon = _windows_text(config.icon)
    hook = _windows_text(config.hook)
    english = _windows_text(anchors.render_root / "English.nsh")
    chinese = _windows_text(anchors.render_root / "SimpChinese.nsh")
    sidecar = _windows_text(config.sidecar)
    _require_ordinal(_one_define(defines, "INSTALLERICON"), icon, "installer icon")
    _require_ordinal(_one_define(defines, "UNINSTALLERICON"), icon, "uninstaller icon")

    includes = _include_values(text)
    for required, field in (
        ("utils.nsh", "utils include"),
        ("FileAssociation.nsh", "FileAssociation include"),
        (hook, "installer hook"),
        (english, "English language include"),
        (chinese, "SimpChinese language include"),
    ):
        if includes.count(required) != 1:
            raise NsisRepackProducerError(f"{field} must occur exactly once")
    absolute_includes = [
        value for value in includes if re.match(r"^[A-Za-z]:[\\/]", value)
    ]
    if absolute_includes != [hook, english, chinese]:
        raise NsisRepackProducerError(
            "absolute includes do not equal hook and languages"
        )

    sidecar_line = f'File "/oname=stock-desk-sidecar.exe" "{sidecar}"'
    _count_exact_line(text, sidecar_line, "x64 sidecar")
    file_absolute_sources = re.findall(
        r'^\s*File(?:\s+/a)?(?:\s+"/oname=[^"\r\n]+")?\s+"'
        r'((?:[A-Za-z]:[\\/]|\\\\)[^"\r\n]+)"\s*$',
        text,
        re.MULTILINE,
    )
    if file_absolute_sources != [sidecar]:
        raise NsisRepackProducerError(
            "absolute File inputs do not equal the x64 sidecar"
        )
    file_lines = _directive_lines(text, "File")
    expected_file_lines = Counter(
        (
            'File "${MAINBINARYSRCPATH}"',
            'File "/oname=$TEMP\\MicrosoftEdgeWebView2RuntimeInstaller.exe" '
            '"${WEBVIEW2INSTALLERPATH}"',
            sidecar_line,
        )
    )
    if Counter(file_lines) != expected_file_lines:
        raise NsisRepackProducerError(
            "File directives do not equal the audited payload set"
        )

    plugin_names = tuple(sorted(set(_PLUGIN_CALL.findall(text))))
    if plugin_names != _EXPECTED_PLUGINS:
        raise NsisRepackProducerError("rendered plugin call set is not the audited set")

    path_uses = (
        PathUse("main-host-unk", main, "payload/main-binary-nss.exe", 1),
        PathUse(
            "additional-plugin-dir",
            plugin_dir,
            "toolchain/Plugins/x86-unicode/additional",
            1,
        ),
        PathUse(
            "offline-webview2",
            webview,
            "payload/webview2-offline-installer.exe",
            1,
        ),
        PathUse("installer-icon", icon, "assets/icon.ico", 1),
        PathUse("uninstaller-icon", icon, "assets/icon.ico", 1),
        PathUse("installer-hook", hook, "includes/installer-hooks.nsh", 1),
        PathUse("language-English", english, "English.nsh", 1),
        PathUse("language-SimpChinese", chinese, "SimpChinese.nsh", 1),
        PathUse(
            "x64-sidecar",
            sidecar,
            "payload/stock-desk-sidecar-x86_64-pc-windows-msvc.exe",
            1,
        ),
    )
    expected_absolute = Counter(
        use.source_absolute for use in path_uses for _ in range(use.occurrences)
    )
    observed_absolute = Counter(_QUOTED_WINDOWS_ABSOLUTE.findall(text))
    if observed_absolute != expected_absolute:
        raise NsisRepackProducerError(
            "unknown absolute path or incorrect absolute-path occurrence count"
        )

    grouped: dict[tuple[str, str], int] = {}
    for use in path_uses:
        key = (use.source_absolute, use.target)
        grouped[key] = grouped.get(key, 0) + use.occurrences
    path_mappings = tuple(
        PathUse("mapping", source, target, occurrences)
        for (source, target), occurrences in sorted(
            grouped.items(), key=lambda item: (-len(item[0][0]), item[0][0])
        )
    )
    return RenderedNsisContract(
        path_uses=path_uses,
        path_mappings=path_mappings,
        webview_relative=webview_relative,
        plugin_names=plugin_names,
    )


def _git_source_identity(repository: Path) -> tuple[str, str, int]:
    def run(*arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise NsisRepackProducerError(
                "checked-out Git identity is unavailable"
            ) from error
        return completed.stdout.strip()

    head = run("rev-parse", "HEAD")
    tree = run("rev-parse", "HEAD^{tree}")
    epoch_text = run("show", "-s", "--format=%ct", "HEAD")
    try:
        epoch = int(epoch_text)
    except ValueError as error:
        raise NsisRepackProducerError("checked-out Git epoch is invalid") from error
    return head, tree, epoch


def validate_source_context(source: ProducerSource, *, repository: Path) -> None:
    """Bind exact event/ref arguments to the checked-out Git source identity."""

    if (
        _SHA.fullmatch(source.source_sha) is None
        or _SHA.fullmatch(source.source_tree) is None
        or _SHA.fullmatch(source.github_sha) is None
        or not isinstance(source.source_epoch, int)
        or isinstance(source.source_epoch, bool)
        or source.source_epoch <= 0
        or source.source_epoch > 2**63 - 1
    ):
        raise NsisRepackProducerError("source context contains an invalid identity")
    if source.event_name == "push":
        if (
            source.source_ref != "refs/heads/main"
            or source.github_sha != source.source_sha
        ):
            raise NsisRepackProducerError(
                "push requires exact protected main source pairing"
            )
    elif source.event_name == "pull_request":
        if _PR_REF.fullmatch(source.source_ref) is None:
            raise NsisRepackProducerError("pull_request requires a canonical merge ref")
    else:
        raise NsisRepackProducerError("source event is not authorized")
    if _git_source_identity(repository) != (
        source.source_sha,
        source.source_tree,
        source.source_epoch,
    ):
        raise NsisRepackProducerError("source context does not equal checked-out Git")


def _require_windows_x64() -> None:
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise NsisRepackProducerError("NSIS repack producer requires Windows x64")
    architecture = os.environ.get("PROCESSOR_ARCHITECTURE", "").upper()
    if architecture not in {"AMD64", "X86_64"}:
        raise NsisRepackProducerError("NSIS repack producer requires Windows x64")


def derive_fixed_anchors(*, local_appdata: Path) -> ProducerAnchors:
    """Derive every live anchor from this tracked script and LOCALAPPDATA."""

    repository = Path(__file__).absolute().parent.parent
    validate_windows_path_text(os.fspath(repository), field="repository")
    validate_windows_path_text(os.fspath(local_appdata), field="LOCALAPPDATA")
    release = repository / "src-tauri/target/x86_64-pc-windows-msvc/release"
    tools = local_appdata / "tauri"
    anchors = ProducerAnchors(
        repository=repository,
        base_config=repository / "src-tauri/tauri.conf.json",
        windows_config=repository / "src-tauri/tauri.windows.conf.json",
        cargo_toml=repository / "src-tauri/Cargo.toml",
        nsis_template=repository / "packaging/nsis/installer.nsi",
        render_root=release / "nsis/x64",
        release_root=release,
        bundle_root=release / "bundle/nsis",
        tauri_tools_root=tools,
        nsis_root=tools / "NSIS",
    )
    for name, path in anchors.__dict__.items():
        validate_windows_path_text(os.fspath(path), field=name)
    return anchors


def _relative(root: Path, path: Path, field: str) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as error:
        raise NsisRepackProducerError(f"{field} escapes its fixed root") from error
    if not relative or relative.startswith("../"):
        raise NsisRepackProducerError(f"{field} is not a strict relative path")
    return relative


def _read_strict_json(path: Path, field: str) -> Mapping[str, object]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise NsisRepackProducerError(f"{field} is unreadable") from error
    return _mapping(parse_json_strict(payload, field=field), field)


def _cargo_package_name(path: Path) -> str:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise NsisRepackProducerError("Cargo.toml is invalid") from error
    package = document.get("package")
    if not isinstance(package, dict) or package.get("name") != "stock-desk-desktop":
        raise NsisRepackProducerError("Cargo package name is not stock-desk-desktop")
    return "stock-desk-desktop"


def _copy_new(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(destination, flags, 0o600)
        with source.open("rb") as input_stream:
            while block := input_stream.read(1024 * 1024):
                remaining = memoryview(block)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("stage copy made no progress")
                    remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError as error:
        raise NsisRepackProducerError("exclusive stage copy failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_new(path: Path, payload: bytes, field: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError as error:
        raise NsisRepackProducerError(
            f"{field} could not be created exclusively"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _stage_inventory(stage: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    file_paths: list[str] = []
    directory_paths: list[str] = []
    for root, walk_directories, walk_files in os.walk(stage):
        walk_directories.sort()
        walk_files.sort()
        root_path = Path(root)
        for name in walk_directories:
            directory_paths.append((root_path / name).relative_to(stage).as_posix())
        for name in walk_files:
            file_paths.append((root_path / name).relative_to(stage).as_posix())
    return (
        tuple(sorted(file_paths, key=lambda value: value.encode())),
        tuple(sorted(directory_paths, key=lambda value: value.encode())),
    )


def _planned_stage_directories(targets: Sequence[str]) -> tuple[str, ...]:
    directories = {
        PurePosixPath(*parts[:index]).as_posix()
        for target in targets
        for parts in (PurePosixPath(target).parts,)
        for index in range(1, len(parts))
    }
    return tuple(sorted(directories, key=lambda value: value.encode()))


def _repository_entries(
    anchors: ProducerAnchors, config: EffectiveNsisConfig
) -> tuple[str, ...]:
    paths = (
        anchors.base_config,
        anchors.windows_config,
        anchors.cargo_toml,
        anchors.nsis_template,
        config.hook,
        config.icon,
        *config.language_sources,
        anchors.render_root,
        anchors.release_root / "stock-desk-desktop.exe",
        config.sidecar,
        anchors.bundle_root,
    )
    return tuple(
        _relative(anchors.repository, path, "repository selection") for path in paths
    )


def _record(
    path: Path, target: str, role: str, *, executable: bool = False
) -> dict[str, object]:
    size, digest = hash_file(path)
    return {
        "path": target,
        "role": role,
        "size": size,
        "sha256": digest,
        "executable": executable,
    }


def _assemble_stage_and_descriptor(
    *,
    work_root: Path,
    source: ProducerSource,
    anchors: ProducerAnchors,
    config: EffectiveNsisConfig,
    repository_snapshot: Path,
    tools_snapshot: Path,
    rendered: RenderedNsisContract,
    original_candidate: Path,
) -> tuple[Path, Path, dict[str, object]]:
    from scripts import nsis_repack_contract as contract

    stage = work_root / "stage"
    prepare_private_directory(stage)
    render_relative = _relative(anchors.repository, anchors.render_root, "render root")
    fixed: tuple[tuple[Path, str, str], ...] = (
        (
            repository_snapshot / "src-tauri/tauri.conf.json",
            "source/tauri.conf.json",
            "tauri-config",
        ),
        (
            repository_snapshot / "src-tauri/tauri.windows.conf.json",
            "source/tauri.windows.conf.json",
            "tauri-config",
        ),
        (
            repository_snapshot / "packaging/nsis/installer.nsi",
            "template/installer.nsi",
            "nsis-template",
        ),
        (
            repository_snapshot / render_relative / "installer.nsi",
            "installer.nsi",
            "nsis-rendered-script",
        ),
        (
            repository_snapshot / render_relative / "FileAssociation.nsh",
            "FileAssociation.nsh",
            "nsis-include",
        ),
        (
            repository_snapshot / render_relative / "utils.nsh",
            "utils.nsh",
            "nsis-include",
        ),
        (
            repository_snapshot / render_relative / "English.nsh",
            "English.nsh",
            "nsis-language",
        ),
        (
            repository_snapshot / render_relative / "SimpChinese.nsh",
            "SimpChinese.nsh",
            "nsis-language",
        ),
        (
            repository_snapshot / _relative(anchors.repository, config.hook, "hook"),
            "includes/installer-hooks.nsh",
            "nsis-hook",
        ),
        (
            repository_snapshot / _relative(anchors.repository, config.icon, "icon"),
            "assets/icon.ico",
            "icon",
        ),
        (
            tools_snapshot / rendered.webview_relative,
            "payload/webview2-offline-installer.exe",
            "webview2",
        ),
        (
            repository_snapshot
            / _relative(anchors.repository, config.sidecar, "sidecar"),
            "payload/stock-desk-sidecar-x86_64-pc-windows-msvc.exe",
            "payload",
        ),
        (
            repository_snapshot
            / _relative(
                anchors.repository,
                anchors.release_root / "stock-desk-desktop.exe",
                "host",
            ),
            "payload/main-binary-nss.exe",
            "payload",
        ),
    )
    planned: dict[str, tuple[str, bool]] = {}
    for source_path, target, role in fixed:
        if target in planned:
            raise NsisRepackProducerError("stage target collision")
        planned[target] = (role, False)
        destination = stage / target
        _copy_new(source_path, destination)
    transformation = patch_tauri_bundle_payload(
        private_root=stage,
        payload=stage / "payload/main-binary-nss.exe",
    )

    nsis_snapshot = tools_snapshot / "NSIS"
    verified = verify_extracted_nsis_toolchain(
        nsis_root=nsis_snapshot,
        additional_plugins_root=nsis_snapshot / "Plugins/x86-unicode/additional",
    )
    for path in sorted(
        nsis_snapshot.rglob("*"), key=lambda item: item.as_posix().encode()
    ):
        if not path.is_file():
            continue
        relative = path.relative_to(nsis_snapshot).as_posix()
        target = f"toolchain/{relative}"
        if target in planned:
            raise NsisRepackProducerError("duplicate toolchain stage target")
        plugin_name = next(
            (name for name, value in _PLUGIN_PATHS.items() if value == target), None
        )
        role = "nsis-plugin" if plugin_name is not None else "nsis-toolchain"
        planned[target] = (role, target == "toolchain/makensis.exe")
        destination = stage / target
        _copy_new(path, destination)

    expected_files = tuple(sorted(planned, key=lambda value: value.encode()))
    expected_directories = _planned_stage_directories(expected_files)
    if _stage_inventory(stage) != (expected_files, expected_directories):
        raise NsisRepackProducerError("stage inventory contains an unplanned entry")
    verify_no_windows_named_streams(stage)
    records = [
        _record(stage / target, target, role, executable=executable)
        for target, (role, executable) in sorted(planned.items())
    ]
    plugin_metadata = []
    for name in rendered.plugin_names:
        target = _PLUGIN_PATHS[name]
        plugin_metadata.append(
            {"name": name, "path": target, "sha256": hash_file(stage / target)[1]}
        )
    candidate_size, candidate_digest = hash_file(original_candidate)
    descriptor: dict[str, object] = {
        "schema_version": 1,
        "source_ref": source.source_ref,
        "source_sha": source.source_sha,
        "source_tree": source.source_tree,
        "source_epoch": source.source_epoch,
        "transformation": transformation,
        "toolchain": {
            "path": "toolchain/makensis.exe",
            "sha256": verified.compiler_sha256,
            "tauri_cli_version": "2.11.4",
            "nsis_version": "3.11",
            "nsis_tauri_utils_version": "0.5.3",
            "plugins": plugin_metadata,
        },
        "argv": [
            "-INPUTCHARSET",
            "UTF8",
            "-OUTPUTCHARSET",
            "UTF8",
            "-V3",
            "installer.nsi",
        ],
        "environment": {"SOURCE_DATE_EPOCH": str(source.source_epoch)},
        "cleared_environment": ["NSISCONFDIR", "NSISDIR"],
        "files": sorted(records, key=lambda item: str(item["path"])),
        "expected_unsigned_installer": {
            "path": "nsis-output.exe",
            "size": candidate_size,
            "sha256": candidate_digest,
        },
        "path_mappings": [
            {
                "source_absolute": mapping.source_absolute,
                "target": mapping.target,
                "occurrences": mapping.occurrences,
            }
            for mapping in rendered.path_mappings
        ],
    }
    contract._normalize_contract(descriptor, manifest=False)
    verify_no_windows_named_streams(stage)
    descriptor_path = work_root / "descriptor.json"
    _write_new(descriptor_path, canonical_json_bytes(descriptor), "producer descriptor")
    return stage, descriptor_path, descriptor


def prepare_producer_stage(
    *, work_root: Path, source: ProducerSource, local_appdata: Path
) -> ProducerResult:
    _require_windows_x64()
    work_root = work_root.absolute()
    if work_root.exists() or work_root.is_symlink():
        raise NsisRepackProducerError("producer work root must not already exist")
    anchors = derive_fixed_anchors(local_appdata=local_appdata)
    validate_source_context(source, repository=anchors.repository)
    base_live = _read_strict_json(anchors.base_config, "base config")
    windows_live = _read_strict_json(anchors.windows_config, "Windows config")
    live_config = validate_effective_config(base_live, windows_live, anchors)
    repository_entries = _repository_entries(anchors, live_config)
    try:
        with private_directory_lease(work_root):
            snapshots = work_root / "snapshots"
            prepare_private_directory(snapshots)
            repository_result = snapshot_artifacts(
                anchors.repository,
                repository_entries,
                snapshots / "repository",
                limits=SnapshotLimits(max_files=1024, max_depth=32),
                allow_windows_hardlinks=False,
                reject_empty_directories=True,
                reject_windows_named_streams=True,
                require_ordinal_paths=True,
            )
            repo = repository_result.root
            base = _read_strict_json(
                repo / "src-tauri/tauri.conf.json", "snapshotted base config"
            )
            windows = _read_strict_json(
                repo / "src-tauri/tauri.windows.conf.json", "snapshotted Windows config"
            )
            config = validate_effective_config(base, windows, anchors)
            cargo_name = _cargo_package_name(repo / "src-tauri/Cargo.toml")
            render_relative = _relative(
                anchors.repository, anchors.render_root, "render root"
            )
            render = repo / render_relative
            render_inventory = validate_render_inventory(
                render,
                english_source=repo
                / _relative(
                    anchors.repository, config.language_sources[0], "English source"
                ),
                chinese_source=repo
                / _relative(
                    anchors.repository, config.language_sources[1], "SimpChinese source"
                ),
            )
            text = (render / "installer.nsi").read_text(encoding="utf-8")
            rendered = analyze_rendered_installer(
                text, config=config, anchors=anchors, cargo_package_name=cargo_name
            )
            bundle = repo / _relative(
                anchors.repository, anchors.bundle_root, "bundle root"
            )
            original_candidate = select_original_candidate(bundle)
            tools_entries = ("NSIS", rendered.webview_relative)
            tools_result = snapshot_artifacts(
                anchors.tauri_tools_root,
                tools_entries,
                snapshots / "tools",
                limits=SnapshotLimits(max_files=1024, max_depth=32),
                allow_windows_hardlinks=False,
                reject_empty_directories=True,
                reject_windows_named_streams=True,
                require_ordinal_paths=True,
            )
            stage, descriptor, _descriptor_value = _assemble_stage_and_descriptor(
                work_root=work_root,
                source=source,
                anchors=anchors,
                config=config,
                repository_snapshot=repo,
                tools_snapshot=tools_result.root,
                rendered=rendered,
                original_candidate=original_candidate,
            )
            descriptor_size, descriptor_digest = hash_file(descriptor)
            receipt_value: dict[str, object] = {
                "schema_version": 1,
                "artifact": "stock-desk-nsis-repack-producer-receipt-v1",
                "source": {
                    "event_name": source.event_name,
                    "source_ref": source.source_ref,
                    "source_sha": source.source_sha,
                    "source_tree": source.source_tree,
                    "source_epoch": source.source_epoch,
                    "github_sha": source.github_sha,
                },
                "repository_selection": list(repository_entries),
                "repository_snapshot": repository_result.summary(),
                "tools_selection": list(tools_entries),
                "tools_snapshot": tools_result.summary(),
                "render_inventory": list(render_inventory),
                "original_candidate": {
                    "path": original_candidate.relative_to(repo).as_posix(),
                    "size": hash_file(original_candidate)[0],
                    "sha256": hash_file(original_candidate)[1],
                },
                "restored_host": {
                    "path": _relative(
                        anchors.repository,
                        anchors.release_root / "stock-desk-desktop.exe",
                        "host",
                    ),
                    "size": hash_file(
                        repo
                        / _relative(
                            anchors.repository,
                            anchors.release_root / "stock-desk-desktop.exe",
                            "host",
                        )
                    )[0],
                    "sha256": hash_file(
                        repo
                        / _relative(
                            anchors.repository,
                            anchors.release_root / "stock-desk-desktop.exe",
                            "host",
                        )
                    )[1],
                },
                "webview_relative": rendered.webview_relative,
                "descriptor": {"size": descriptor_size, "sha256": descriptor_digest},
            }
            receipt = work_root / "producer-receipt.json"
            _write_new(receipt, canonical_json_bytes(receipt_value), "producer receipt")
    except (SecureArtifactSnapshotError, OSError) as error:
        raise NsisRepackProducerError(
            f"secure producer stage creation failed: {error}"
        ) from error
    return ProducerResult(
        stage=stage,
        descriptor=descriptor,
        producer_receipt=receipt,
        original_candidate=original_candidate,
    )


def _remove_private_tree(path: Path) -> None:
    if not path.exists():
        return
    for root, directories, files in os.walk(path, topdown=False):
        for name in files:
            os.chmod(Path(root) / name, 0o600)
        for name in directories:
            os.chmod(Path(root) / name, 0o700)
        os.chmod(root, 0o700)
    shutil.rmtree(path)


def verify_live_producer_inputs(
    *, work_root: Path, source: ProducerSource, local_appdata: Path
) -> dict[str, object]:
    """Re-snapshot every selected live input and compare the private receipt."""

    _require_windows_x64()
    work_root = work_root.absolute()
    receipt_path = work_root / "producer-receipt.json"
    try:
        receipt_bytes = receipt_path.read_bytes()
    except OSError as error:
        raise NsisRepackProducerError("producer receipt is unavailable") from error
    receipt = _mapping(
        parse_json_strict(receipt_bytes, field="producer receipt"), "producer receipt"
    )
    if receipt_bytes != canonical_json_bytes(receipt):
        raise NsisRepackProducerError("producer receipt is not canonical JSON")
    if set(receipt) != _PRODUCER_RECEIPT_KEYS:
        raise NsisRepackProducerError("producer receipt members changed")
    _exact(receipt.get("schema_version"), 1, "producer receipt schema_version")
    _exact(
        receipt.get("artifact"),
        "stock-desk-nsis-repack-producer-receipt-v1",
        "producer receipt artifact",
    )
    expected_source = {
        "event_name": source.event_name,
        "source_ref": source.source_ref,
        "source_sha": source.source_sha,
        "source_tree": source.source_tree,
        "source_epoch": source.source_epoch,
        "github_sha": source.github_sha,
    }
    if receipt.get("source") != expected_source:
        raise NsisRepackProducerError("producer receipt source context changed")
    anchors = derive_fixed_anchors(local_appdata=local_appdata)
    validate_source_context(source, repository=anchors.repository)
    base = _read_strict_json(anchors.base_config, "base config")
    windows = _read_strict_json(anchors.windows_config, "Windows config")
    config = validate_effective_config(base, windows, anchors)
    repository_entries = _repository_entries(anchors, config)
    if receipt.get("repository_selection") != list(repository_entries):
        raise NsisRepackProducerError("producer receipt repository selection changed")
    try:
        descriptor_size, descriptor_digest = hash_file(work_root / "descriptor.json")
    except NsisRepackProducerError as error:
        raise NsisRepackProducerError(
            "producer descriptor identity is unavailable"
        ) from error
    if receipt.get("descriptor") != {
        "size": descriptor_size,
        "sha256": descriptor_digest,
    }:
        raise NsisRepackProducerError("producer receipt descriptor identity changed")
    recheck = work_root / ".live-recheck"
    if recheck.exists() or recheck.is_symlink():
        raise NsisRepackProducerError("live recheck root already exists")
    try:
        prepare_private_directory(recheck)
        repo_result = snapshot_artifacts(
            anchors.repository,
            repository_entries,
            recheck / "repository",
            limits=SnapshotLimits(max_files=1024, max_depth=32),
            allow_windows_hardlinks=False,
            reject_empty_directories=True,
            reject_windows_named_streams=True,
            require_ordinal_paths=True,
        )
        repo = repo_result.root
        snapshot_base = _read_strict_json(
            repo / "src-tauri/tauri.conf.json", "rechecked base config"
        )
        snapshot_windows = _read_strict_json(
            repo / "src-tauri/tauri.windows.conf.json", "rechecked Windows config"
        )
        snapshot_config = validate_effective_config(
            snapshot_base, snapshot_windows, anchors
        )
        if _repository_entries(anchors, snapshot_config) != repository_entries:
            raise NsisRepackProducerError(
                "producer receipt repository selection changed"
            )
        cargo_name = _cargo_package_name(repo / "src-tauri/Cargo.toml")
        render_relative = _relative(
            anchors.repository, anchors.render_root, "render root"
        )
        render = repo / render_relative
        render_inventory = validate_render_inventory(
            render,
            english_source=repo
            / _relative(
                anchors.repository,
                snapshot_config.language_sources[0],
                "English source",
            ),
            chinese_source=repo
            / _relative(
                anchors.repository,
                snapshot_config.language_sources[1],
                "SimpChinese source",
            ),
        )
        if receipt.get("render_inventory") != list(render_inventory):
            raise NsisRepackProducerError("producer receipt render inventory changed")
        rendered = analyze_rendered_installer(
            (render / "installer.nsi").read_text(encoding="utf-8"),
            config=snapshot_config,
            anchors=anchors,
            cargo_package_name=cargo_name,
        )
        if receipt.get("webview_relative") != rendered.webview_relative:
            raise NsisRepackProducerError("producer receipt WebView selection changed")
        tools_entries = ("NSIS", rendered.webview_relative)
        if receipt.get("tools_selection") != list(tools_entries):
            raise NsisRepackProducerError("producer receipt tools selection changed")
        bundle = repo / _relative(
            anchors.repository, anchors.bundle_root, "bundle root"
        )
        original_candidate = select_original_candidate(bundle)
        candidate_size, candidate_digest = hash_file(original_candidate)
        if receipt.get("original_candidate") != {
            "path": original_candidate.relative_to(repo).as_posix(),
            "size": candidate_size,
            "sha256": candidate_digest,
        }:
            raise NsisRepackProducerError(
                "producer receipt original candidate identity changed"
            )
        host_relative = _relative(
            anchors.repository,
            anchors.release_root / "stock-desk-desktop.exe",
            "host",
        )
        host_size, host_digest = hash_file(repo / host_relative)
        if receipt.get("restored_host") != {
            "path": host_relative,
            "size": host_size,
            "sha256": host_digest,
        }:
            raise NsisRepackProducerError(
                "producer receipt restored host identity changed"
            )
        tools_result = snapshot_artifacts(
            anchors.tauri_tools_root,
            tools_entries,
            recheck / "tools",
            limits=SnapshotLimits(max_files=1024, max_depth=32),
            allow_windows_hardlinks=False,
            reject_empty_directories=True,
            reject_windows_named_streams=True,
            require_ordinal_paths=True,
        )
        if (
            receipt.get("repository_snapshot") != repo_result.summary()
            or receipt.get("tools_snapshot") != tools_result.summary()
        ):
            raise NsisRepackProducerError("live producer inputs changed after capture")
    except (SecureArtifactSnapshotError, OSError) as error:
        raise NsisRepackProducerError(
            f"live producer inputs could not be secured: {error}"
        ) from error
    finally:
        try:
            _remove_private_tree(recheck)
        except OSError as error:
            raise NsisRepackProducerError(
                "live producer recheck cleanup failed"
            ) from error
    return {"schema": "stock-desk-nsis-repack-live-inputs-v1", "status": "verified"}


def _source_from_arguments(arguments: argparse.Namespace) -> ProducerSource:
    return ProducerSource(
        event_name=arguments.event_name,
        source_ref=arguments.source_ref,
        source_sha=arguments.source_sha,
        source_tree=arguments.source_tree,
        source_epoch=arguments.source_epoch,
        github_sha=arguments.github_sha,
    )


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--event-name", choices=("push", "pull_request"), required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--source-epoch", type=int, required=True)
    parser.add_argument("--github-sha", required=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-stage")
    verify = commands.add_parser("verify-live-inputs")
    _add_source_arguments(prepare)
    _add_source_arguments(verify)
    arguments = parser.parse_args(argv)
    local_text = os.environ.get("LOCALAPPDATA")
    if not local_text:
        raise NsisRepackProducerError("LOCALAPPDATA is required")
    source = _source_from_arguments(arguments)
    summary: dict[str, object]
    if arguments.command == "prepare-stage":
        result = prepare_producer_stage(
            work_root=arguments.work_root,
            source=source,
            local_appdata=Path(local_text),
        )
        root = arguments.work_root.absolute()
        summary = {
            "schema": "stock-desk-nsis-repack-producer-summary-v1",
            "stage": result.stage.relative_to(root).as_posix(),
            "descriptor": result.descriptor.relative_to(root).as_posix(),
            "producer_receipt": result.producer_receipt.relative_to(root).as_posix(),
            "original_candidate": result.original_candidate.relative_to(
                root
            ).as_posix(),
        }
    else:
        summary = verify_live_producer_inputs(
            work_root=arguments.work_root,
            source=source,
            local_appdata=Path(local_text),
        )
    print(canonical_json_bytes(summary).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NsisRepackProducerError as error:
        print(f"NSIS repack producer rejected input: {error}", file=sys.stderr)
        raise SystemExit(1) from error
