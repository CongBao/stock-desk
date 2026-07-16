from __future__ import annotations

from copy import deepcopy
import inspect
import json
from pathlib import Path

import pytest

from scripts import nsis_repack_producer as producer
from scripts import nsis_repack_contract as contract


def _anchors() -> producer.ProducerAnchors:
    repository = Path("C:/repo")
    release = repository / "src-tauri/target/x86_64-pc-windows-msvc/release"
    tools = Path("C:/Users/operator/AppData/Local/tauri")
    return producer.ProducerAnchors(
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


def _config_documents() -> tuple[dict[str, object], dict[str, object]]:
    repository = Path(__file__).resolve().parents[2]
    base = json.loads((repository / "src-tauri/tauri.conf.json").read_text())
    windows = json.loads((repository / "src-tauri/tauri.windows.conf.json").read_text())
    assert isinstance(base, dict)
    assert isinstance(windows, dict)
    return base, windows


def _windows(path: Path) -> str:
    return str(path).replace("/", "\\")


def _rendered(config: producer.EffectiveNsisConfig) -> str:
    anchors = _anchors()
    release = anchors.release_root
    render = anchors.render_root
    tools = anchors.tauri_tools_root
    return "\n".join(
        (
            '!define MAINBINARYNAME "stock-desk-desktop"',
            f'!define MAINBINARYSRCPATH "{_windows(release / "stock-desk-desktop.exe")}"',
            f'!define ADDITIONALPLUGINSPATH "{_windows(anchors.nsis_root / "Plugins/x86-unicode/additional")}"',
            '!define INSTALLWEBVIEW2MODE "offlineInstaller"',
            f'!define WEBVIEW2INSTALLERPATH "{_windows(tools / "x64/01234567-89ab-cdef-0123-456789abcdef/MicrosoftEdgeWebView2RuntimeInstallerX64.exe")}"',
            f'!define INSTALLERICON "{_windows(config.icon)}"',
            f'!define UNINSTALLERICON "{_windows(config.icon)}"',
            '!define WEBVIEW2BOOTSTRAPPERPATH ""',
            '!define MINIMUMWEBVIEW2VERSION ""',
            '!define LICENSE ""',
            '!define SIDEBARIMAGE ""',
            '!define HEADERIMAGE ""',
            '!define UNINSTALLERHEADERIMAGE ""',
            '!define UNINSTALLERSIGNCOMMAND ""',
            '!define OUTFILE "nsis-output.exe"',
            'OutFile "${OUTFILE}"',
            '!addplugindir "${ADDITIONALPLUGINSPATH}"',
            '!include "utils.nsh"',
            '!include "FileAssociation.nsh"',
            f'!include "{_windows(config.hook)}"',
            f'!include "{_windows(render / "English.nsh")}"',
            f'!include "{_windows(render / "SimpChinese.nsh")}"',
            'File "${MAINBINARYSRCPATH}"',
            'File "/oname=$TEMP\\MicrosoftEdgeWebView2RuntimeInstaller.exe" "${WEBVIEW2INSTALLERPATH}"',
            f'File "/oname=stock-desk-sidecar.exe" "{_windows(config.sidecar)}"',
            'NSISdl::download "https://example.invalid" "$TEMP\\wv2.exe"',
            'System::Call "kernel32::GetCurrentProcess() p .r0"',
            "nsDialogs::Create 1018",
            'nsis_tauri_utils::SemverCompare "1" "1"',
            "",
        )
    )


@pytest.mark.parametrize(
    ("base", "patch", "expected"),
    [
        ({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}}, {"a": {"b": 3, "c": 2}}),
        ({"a": [1, 2]}, {"a": [3]}, {"a": [3]}),
        ({"a": 1, "b": 2}, {"a": None}, {"b": 2}),
        ({"a": {"b": 1}}, {"a": "replacement"}, {"a": "replacement"}),
        ({"a": 1}, ["replacement"], ["replacement"]),
    ],
)
def test_merge_rfc7396_matches_json_merge_patch(
    base: object, patch: object, expected: object
) -> None:
    before_base = deepcopy(base)
    before_patch = deepcopy(patch)

    assert producer.merge_rfc7396(base, patch) == expected
    assert base == before_base
    assert patch == before_patch


def test_strict_json_rejects_duplicate_members() -> None:
    with pytest.raises(producer.NsisRepackProducerError, match="duplicate JSON"):
        producer.parse_json_strict(b'{"bundle":{},"bundle":{}}', field="config")


def test_validate_effective_config_derives_the_closed_windows_x64_contract() -> None:
    base, windows = _config_documents()

    config = producer.validate_effective_config(base, windows, _anchors())

    assert config.icon == Path("C:/repo/src-tauri/icons/icon.ico")
    assert config.hook == Path("C:/repo/packaging/nsis/installer-hooks.nsh")
    assert config.template == Path("C:/repo/packaging/nsis/installer.nsi")
    assert config.languages == ("English", "SimpChinese")
    assert config.language_sources == (
        Path("C:/repo/packaging/nsis/languages/English.nsh"),
        Path("C:/repo/packaging/nsis/languages/SimpChinese.nsh"),
    )
    assert config.sidecar == Path(
        "C:/repo/src-tauri/binaries/stock-desk-sidecar-x86_64-pc-windows-msvc.exe"
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data[0]["bundle"].__setitem__("resources", []), "resources"),
        (
            lambda data: data[0]["bundle"]["windows"].__setitem__(
                "webviewInstallMode", {"type": "downloadBootstrapper"}
            ),
            "offlineInstaller",
        ),
        (
            lambda data: data[0]["bundle"]["windows"].__setitem__(
                "certificateThumbprint", "present"
            ),
            "signing",
        ),
        (
            lambda data: data[0]["bundle"]["windows"]["nsis"].__setitem__(
                "installerIcon", "icons/other.ico"
            ),
            "icon",
        ),
        (
            lambda data: data[0]["bundle"]["windows"]["nsis"].__setitem__(
                "languages", ["SimpChinese", "English"]
            ),
            "languages",
        ),
        (
            lambda data: data[1]["bundle"].__setitem__(
                "externalBin", ["binaries/another-sidecar"]
            ),
            "externalBin",
        ),
        (lambda data: data[0]["bundle"].__setitem__("targets", ["msi"]), "targets"),
    ],
)
def test_validate_effective_config_rejects_review_expanding_changes(
    mutation: object, message: str
) -> None:
    base, windows = _config_documents()
    documents = [base, windows]
    assert callable(mutation)
    mutation(documents)

    with pytest.raises(producer.NsisRepackProducerError, match=message):
        producer.validate_effective_config(base, windows, _anchors())


def test_analyze_rendered_installer_returns_exact_role_multiset() -> None:
    base, windows = _config_documents()
    config = producer.validate_effective_config(base, windows, _anchors())

    contract = producer.analyze_rendered_installer(
        _rendered(config),
        config=config,
        anchors=_anchors(),
        cargo_package_name="stock-desk-desktop",
    )

    assert [use.purpose for use in contract.path_uses] == [
        "main-host-unk",
        "additional-plugin-dir",
        "offline-webview2",
        "installer-icon",
        "uninstaller-icon",
        "installer-hook",
        "language-English",
        "language-SimpChinese",
        "x64-sidecar",
    ]
    assert contract.webview_relative == (
        "x64/01234567-89ab-cdef-0123-456789abcdef/"
        "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    )
    assert contract.plugin_names == (
        "NSISdl",
        "System",
        "nsDialogs",
        "nsis_tauri_utils",
    )
    assert contract.path_mappings[-1].target == "assets/icon.ico"
    assert contract.path_mappings[-1].occurrences == 2


@pytest.mark.parametrize(
    ("replace", "message"),
    [
        (
            ("stock-desk-desktop.exe", "stock-desk-sidecar-x86_64-pc-windows-msvc.exe"),
            "MAINBINARYSRCPATH",
        ),
        (
            (
                '!define MAINBINARYSRCPATH "C:\\repo\\src-tauri\\target\\x86_64-pc-windows-msvc\\release\\stock-desk-desktop.exe"',
                '!define MAINBINARYSRCPATH "C:\\repo\\src-tauri\\target\\x86_64-pc-windows-msvc\\release\\stock-desk-desktop.exe"\n'
                '!define MAINBINARYSRCPATH "C:\\repo\\src-tauri\\target\\x86_64-pc-windows-msvc\\release\\stock-desk-desktop.exe"',
            ),
            "MAINBINARYSRCPATH",
        ),
        (
            ("NSIS\\Plugins\\x86-unicode\\additional", "fake\\Plugins\\additional"),
            "ADDITIONALPLUGINSPATH",
        ),
        (
            ('!addplugindir "${ADDITIONALPLUGINSPATH}"', '!addplugindir "C:\\extra"'),
            "plugin",
        ),
        (("offlineInstaller", "downloadBootstrapper"), "offlineInstaller"),
        (("icons\\icon.ico", "icons\\other.ico"), "icon"),
        (("installer-hooks.nsh", "other-hooks.nsh"), "hook"),
        (
            (
                '!include "C:\\repo\\src-tauri\\target\\x86_64-pc-windows-msvc\\release\\nsis\\x64\\English.nsh"',
                "",
            ),
            "English",
        ),
        (
            ("stock-desk-sidecar-x86_64-pc-windows-msvc.exe", "stock-desk-sidecar.exe"),
            "sidecar",
        ),
        (("/oname=stock-desk-sidecar.exe", "/oname=renamed.exe"), "sidecar"),
        (
            (
                'File "${MAINBINARYSRCPATH}"',
                'File "${MAINBINARYSRCPATH}"\nFile /a "/oname=extra.exe" "C:\\exists\\extra.exe"',
            ),
            "absolute",
        ),
        (
            (
                '!include "utils.nsh"',
                '!include "utils.nsh"\n!include "C:\\exists\\unknown.nsh"',
            ),
            "absolute",
        ),
        (
            (
                '!include "utils.nsh"',
                '!include "utils.nsh"\n!include C:\\exists\\unknown.nsh',
            ),
            "include",
        ),
        (
            (
                '!addplugindir "${ADDITIONALPLUGINSPATH}"',
                '!addplugindir "${ADDITIONALPLUGINSPATH}"\n'
                '!addplugindir "toolchain\\Plugins\\x86-unicode"',
            ),
            "plugin",
        ),
        (
            (
                'File "${MAINBINARYSRCPATH}"',
                'File "${MAINBINARYSRCPATH}"\nFile "payload\\main-binary-nss.exe"',
            ),
            "File",
        ),
    ],
)
def test_analyze_rendered_installer_rejects_malicious_existing_path_families(
    replace: tuple[str, str], message: str
) -> None:
    base, windows = _config_documents()
    config = producer.validate_effective_config(base, windows, _anchors())
    original, malicious = replace
    text = _rendered(config).replace(original, malicious, 1)

    with pytest.raises(producer.NsisRepackProducerError, match=message):
        producer.analyze_rendered_installer(
            text,
            config=config,
            anchors=_anchors(),
            cargo_package_name="stock-desk-desktop",
        )


@pytest.mark.parametrize(
    "source",
    [
        producer.ProducerSource(
            "push", "refs/pull/1/merge", "a" * 40, "b" * 40, 1, "a" * 40
        ),
        producer.ProducerSource(
            "pull_request", "refs/heads/main", "a" * 40, "b" * 40, 1, "c" * 40
        ),
        producer.ProducerSource(
            "pull_request", "refs/pull/01/merge", "a" * 40, "b" * 40, 1, "c" * 40
        ),
        producer.ProducerSource(
            "push", "refs/heads/main", "a" * 40, "b" * 40, 1, "c" * 40
        ),
    ],
)
def test_validate_source_context_rejects_invalid_event_ref_or_main_sha_pair(
    source: producer.ProducerSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        producer,
        "_git_source_identity",
        lambda _repository: ("a" * 40, "b" * 40, 1),
    )

    with pytest.raises(producer.NsisRepackProducerError):
        producer.validate_source_context(source, repository=Path("C:/repo"))


def test_validate_source_context_rejects_epoch_outside_signed_64_bit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = producer.ProducerSource(
        "push",
        "refs/heads/main",
        "a" * 40,
        "b" * 40,
        2**63,
        "a" * 40,
    )
    monkeypatch.setattr(
        producer,
        "_git_source_identity",
        lambda _repository: (
            source.source_sha,
            source.source_tree,
            source.source_epoch,
        ),
    )

    with pytest.raises(producer.NsisRepackProducerError, match="invalid identity"):
        producer.validate_source_context(source, repository=Path("C:/repo"))


@pytest.mark.parametrize(
    "path_text",
    [
        r"\\server\share\tauri",
        r"\\?\C:\Users\operator\tauri",
        r"C:\Users\operator\tauri:evil",
        r"C:\Users\operator\..\tauri",
        r"C:/Users/operator/tauri",
        r"C:\Users\operator\tauri\.",
    ],
)
def test_windows_anchor_text_rejects_alias_unc_ads_and_dot_components(
    path_text: str,
) -> None:
    with pytest.raises(producer.NsisRepackProducerError):
        producer.validate_windows_path_text(path_text, field="anchor")


def _render_inventory(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    (root / "installer.nsi").write_text("rendered\n", encoding="utf-8")
    (root / "FileAssociation.nsh").write_text("association\n", encoding="utf-8")
    (root / "utils.nsh").write_text("utils\n", encoding="utf-8")
    english = root.parent / "English-source.nsh"
    chinese = root.parent / "SimpChinese-source.nsh"
    english.write_bytes(b"english\n")
    chinese.write_bytes(b"chinese\n")
    (root / "English.nsh").write_bytes(b"\xef\xbb\xbf" + english.read_bytes())
    (root / "SimpChinese.nsh").write_bytes(b"\xef\xbb\xbf" + chinese.read_bytes())
    return english, chinese


def test_validate_render_inventory_accepts_only_exact_five_files(
    tmp_path: Path,
) -> None:
    render = tmp_path / "render"
    english, chinese = _render_inventory(render)

    assert producer.validate_render_inventory(
        render, english_source=english, chinese_source=chinese
    ) == (
        "FileAssociation.nsh",
        "English.nsh",
        "SimpChinese.nsh",
        "installer.nsi",
        "utils.nsh",
    )


@pytest.mark.parametrize("extra_kind", ["file", "directory", "symlink"])
def test_validate_render_inventory_rejects_sixth_or_nonordinary_entry(
    tmp_path: Path, extra_kind: str
) -> None:
    render = tmp_path / "render"
    english, chinese = _render_inventory(render)
    extra = render / "hidden-extra"
    if extra_kind == "file":
        extra.write_bytes(b"present")
    elif extra_kind == "directory":
        extra.mkdir()
    else:
        extra.symlink_to(render / "utils.nsh")

    with pytest.raises(producer.NsisRepackProducerError, match="render inventory"):
        producer.validate_render_inventory(
            render, english_source=english, chinese_source=chinese
        )


def test_validate_render_inventory_rejects_language_copy_without_utf8_bom(
    tmp_path: Path,
) -> None:
    render = tmp_path / "render"
    english, chinese = _render_inventory(render)
    (render / "English.nsh").write_bytes(english.read_bytes())

    with pytest.raises(producer.NsisRepackProducerError, match="English"):
        producer.validate_render_inventory(
            render, english_source=english, chinese_source=chinese
        )


def test_select_original_candidate_requires_one_direct_exe(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    expected = bundle / "stock-desk.exe"
    expected.write_bytes(b"candidate")

    assert producer.select_original_candidate(bundle) == expected


@pytest.mark.parametrize("shape", ["second", "nested-only", "nested-second"])
def test_select_original_candidate_rejects_ambiguous_or_nested_candidates(
    tmp_path: Path, shape: str
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    if shape != "nested-only":
        (bundle / "stock-desk.exe").write_bytes(b"candidate")
    if shape == "second":
        (bundle / "other.exe").write_bytes(b"candidate")
    else:
        child = bundle / "child"
        child.mkdir()
        (child / "nested.exe").write_bytes(b"candidate")

    with pytest.raises(producer.NsisRepackProducerError, match="candidate"):
        producer.select_original_candidate(bundle)


def test_production_entry_points_do_not_accept_anchor_or_candidate_overrides() -> None:
    parameters = inspect.signature(producer.prepare_producer_stage).parameters

    assert tuple(parameters) == ("work_root", "source", "local_appdata")


def _install_fake_toolchain(root: Path, lock: Path) -> None:
    plugin_paths = {
        "Plugins/x86-unicode/NSISdl.dll",
        "Plugins/x86-unicode/System.dll",
        "Plugins/x86-unicode/nsDialogs.dll",
        "Plugins/x86-unicode/additional/nsis_tauri_utils.dll",
    }
    paths = {
        path.removeprefix("toolchain/") for path in contract._REQUIRED_TOOLCHAIN_PATHS
    } | plugin_paths
    records: list[dict[str, object]] = []
    for relative in sorted(paths):
        payload = f"fake:{relative}\n".encode()
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        role = "nsis-plugin" if relative in plugin_paths else "nsis-toolchain"
        records.append(
            {
                "path": f"toolchain/{relative}",
                "role": role,
                "size": len(payload),
                "sha256": producer.hash_file(destination)[1],
                "executable": relative == "makensis.exe",
            }
        )
    document = {
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
        "extracted_tree": contract._canonical_toolchain_tree(records),
    }
    lock.write_bytes(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )


def _prepare_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[producer.ProducerAnchors, producer.ProducerSource, Path]:
    repository = tmp_path / "repo"
    release = repository / "src-tauri/target/x86_64-pc-windows-msvc/release"
    render = release / "nsis/x64"
    bundle = release / "bundle/nsis"
    tools = tmp_path / "local/tauri"
    nsis = tools / "NSIS"
    for directory in (
        repository / "src-tauri",
        repository / "packaging/nsis/languages",
        repository / "src-tauri/icons",
        repository / "src-tauri/binaries",
        render,
        bundle,
        tools / "x64/01234567-89ab-cdef-0123-456789abcdef",
        nsis,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    base, windows = _config_documents()
    (repository / "src-tauri/tauri.conf.json").write_text(json.dumps(base))
    (repository / "src-tauri/tauri.windows.conf.json").write_text(json.dumps(windows))
    (repository / "src-tauri/Cargo.toml").write_text(
        '[package]\nname = "stock-desk-desktop"\n', encoding="utf-8"
    )
    (repository / "packaging/nsis/installer.nsi").write_text("template\n")
    (repository / "packaging/nsis/installer-hooks.nsh").write_text("hook\n")
    english_source = repository / "packaging/nsis/languages/English.nsh"
    chinese_source = repository / "packaging/nsis/languages/SimpChinese.nsh"
    english_source.write_bytes(b"english\n")
    chinese_source.write_bytes(b"chinese\n")
    (repository / "src-tauri/icons/icon.ico").write_bytes(b"icon\n")
    sidecar = (
        repository / "src-tauri/binaries/stock-desk-sidecar-x86_64-pc-windows-msvc.exe"
    )
    sidecar.write_bytes(b"sidecar\n")
    host = release / "stock-desk-desktop.exe"
    host.write_bytes(b"before:" + contract.TAURI_BUNDLE_MARKER_UNKNOWN + b":after")
    (bundle / "stock-desk-setup.exe").write_bytes(b"unsigned-candidate\n")
    (render / "FileAssociation.nsh").write_bytes(b"association\n")
    (render / "utils.nsh").write_bytes(b"utils\n")
    (render / "English.nsh").write_bytes(b"\xef\xbb\xbf" + english_source.read_bytes())
    (render / "SimpChinese.nsh").write_bytes(
        b"\xef\xbb\xbf" + chinese_source.read_bytes()
    )
    webview = (
        tools / "x64/01234567-89ab-cdef-0123-456789abcdef/"
        "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    )
    webview.write_bytes(b"webview\n")
    anchors = producer.ProducerAnchors(
        repository=repository,
        base_config=repository / "src-tauri/tauri.conf.json",
        windows_config=repository / "src-tauri/tauri.windows.conf.json",
        cargo_toml=repository / "src-tauri/Cargo.toml",
        nsis_template=repository / "packaging/nsis/installer.nsi",
        render_root=render,
        release_root=release,
        bundle_root=bundle,
        tauri_tools_root=tools,
        nsis_root=nsis,
    )
    config = producer.validate_effective_config(base, windows, anchors)

    def rendered_path(path: Path) -> str:
        if path.is_relative_to(repository):
            relative = path.relative_to(repository)
            return "C:\\repo" + (
                "\\" + str(relative).replace("/", "\\") if str(relative) != "." else ""
            )
        relative = path.relative_to(tools)
        return "D:\\tauri" + (
            "\\" + str(relative).replace("/", "\\") if str(relative) != "." else ""
        )

    monkeypatch.setattr(producer, "_windows_text", rendered_path)
    rendered_text = _rendered(config)
    for actual, expected in (
        ("C:\\repo", rendered_path(repository)),
        ("C:\\Users\\operator\\AppData\\Local\\tauri", rendered_path(tools)),
        (_windows(config.icon), rendered_path(config.icon)),
        (_windows(config.hook), rendered_path(config.hook)),
        (_windows(config.sidecar), rendered_path(config.sidecar)),
    ):
        rendered_text = rendered_text.replace(actual, expected)
    (render / "installer.nsi").write_text(rendered_text, encoding="utf-8")
    lock = tmp_path / "toolchain-lock.json"
    _install_fake_toolchain(nsis, lock)
    monkeypatch.setattr(contract, "TOOLCHAIN_LOCK_PATH", lock)
    monkeypatch.setattr(producer, "derive_fixed_anchors", lambda **_kwargs: anchors)
    monkeypatch.setattr(
        producer, "validate_source_context", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(producer, "_require_windows_x64", lambda: None)
    source = producer.ProducerSource(
        "push", "refs/heads/main", "a" * 40, "b" * 40, 123, "a" * 40
    )
    return anchors, source, tmp_path / "producer-work"


def test_prepare_stage_writes_canonical_task2a_descriptor_and_closed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _anchors_fixture, source, work = _prepare_fixture(tmp_path, monkeypatch)

    result = producer.prepare_producer_stage(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )

    assert result.stage == work / "stage"
    assert result.descriptor.read_bytes() == producer.canonical_json_bytes(
        json.loads(result.descriptor.read_bytes())
    )
    assert not (result.stage / "captured").exists()
    patched = result.stage / contract.TAURI_TRANSFORMED_PAYLOAD_PATH
    assert contract.TAURI_BUNDLE_MARKER_UNKNOWN not in patched.read_bytes()
    assert patched.read_bytes().count(contract.TAURI_BUNDLE_MARKER_NSIS) == 1
    plugin = "toolchain/Plugins/x86-unicode/additional/nsis_tauri_utils.dll"
    assert (
        sum(
            path.relative_to(result.stage).as_posix() == plugin
            for path in result.stage.rglob("*")
        )
        == 1
    )
    kit = tmp_path / "kit"
    created = contract.create_kit(
        descriptor=result.descriptor,
        source_root=result.stage,
        output=kit,
        expected_source_ref=source.source_ref,
        expected_source_sha=source.source_sha,
        expected_source_tree=source.source_tree,
        expected_source_epoch=source.source_epoch,
    )
    verified = contract.verify_kit(
        kit=kit,
        expected_source_ref=source.source_ref,
        expected_source_sha=source.source_sha,
        expected_source_tree=source.source_tree,
        expected_source_epoch=source.source_epoch,
        expected_kit_sha256=str(created["kit_sha256"]),
    )
    assert verified["kit_sha256"] == created["kit_sha256"]


def test_prepare_derives_descriptor_records_after_final_closed_stage_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _anchors_fixture, source, work = _prepare_fixture(tmp_path, monkeypatch)
    events: list[str] = []
    original_record = producer._record

    def verified(_stage: Path) -> None:
        events.append("closed-stage-check")

    def recorded(
        path: Path, target: str, role: str, *, executable: bool = False
    ) -> dict[str, object]:
        events.append("descriptor-record")
        return original_record(path, target, role, executable=executable)

    monkeypatch.setattr(producer, "verify_no_windows_named_streams", verified)
    monkeypatch.setattr(producer, "_record", recorded)

    producer.prepare_producer_stage(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )

    assert events[0] == "closed-stage-check"
    assert "descriptor-record" in events


def test_prepare_rejects_an_unplanned_empty_stage_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _anchors_fixture, source, work = _prepare_fixture(tmp_path, monkeypatch)
    original_copy = producer._copy_new
    injected = False

    def inject_empty_directory(source_path: Path, destination: Path) -> None:
        nonlocal injected
        original_copy(source_path, destination)
        if not injected:
            injected = True
            (work / "stage/unplanned-empty").mkdir()

    monkeypatch.setattr(producer, "_copy_new", inject_empty_directory)

    with pytest.raises(producer.NsisRepackProducerError, match="stage inventory"):
        producer.prepare_producer_stage(
            work_root=work, source=source, local_appdata=tmp_path / "local"
        )
    assert injected is True
    assert not work.exists()


def test_verify_live_inputs_rejects_workspace_change_after_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchors, source, work = _prepare_fixture(tmp_path, monkeypatch)
    producer.prepare_producer_stage(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )

    summary = producer.verify_live_producer_inputs(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )
    assert summary == {
        "schema": "stock-desk-nsis-repack-live-inputs-v1",
        "status": "verified",
    }
    (anchors.render_root / "utils.nsh").write_bytes(b"changed\n")
    with pytest.raises(producer.NsisRepackProducerError, match="live producer inputs"):
        producer.verify_live_producer_inputs(
            work_root=work, source=source, local_appdata=tmp_path / "local"
        )


def test_prepare_uses_snapshot_and_final_recheck_rejects_source_change_during_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchors, source, work = _prepare_fixture(tmp_path, monkeypatch)
    original_copy = producer._copy_new
    copy_count = 0

    def mutate_live_source_after_snapshot(source_path: Path, destination: Path) -> None:
        nonlocal copy_count
        original_copy(source_path, destination)
        copy_count += 1
        if copy_count == 1:
            (anchors.render_root / "utils.nsh").write_bytes(
                b"changed-during-assembly\n"
            )

    monkeypatch.setattr(producer, "_copy_new", mutate_live_source_after_snapshot)

    result = producer.prepare_producer_stage(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )

    assert (result.stage / "utils.nsh").read_bytes() == b"utils\n"
    with pytest.raises(producer.NsisRepackProducerError, match="live producer inputs"):
        producer.verify_live_producer_inputs(
            work_root=work, source=source, local_appdata=tmp_path / "local"
        )


def test_producer_uses_locked_windows_hardlink_policy_for_native_build_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _anchors_fixture, source, work = _prepare_fixture(tmp_path, monkeypatch)
    original_snapshot = producer.snapshot_artifacts
    observed_policies: list[bool] = []

    def record_snapshot_policy(*args: object, **kwargs: object) -> object:
        observed_policies.append(bool(kwargs["allow_windows_hardlinks"]))
        return original_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(producer, "snapshot_artifacts", record_snapshot_policy)

    producer.prepare_producer_stage(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )
    producer.verify_live_producer_inputs(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )

    assert observed_policies == [True, True, True, True]


@pytest.mark.parametrize(
    "mutation",
    [
        "schema-version",
        "artifact",
        "render-inventory",
        "original-candidate",
        "restored-host",
        "descriptor",
        "extra-member",
    ],
)
def test_verify_live_inputs_rejects_rewritten_receipt_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    _anchors_fixture, source, work = _prepare_fixture(tmp_path, monkeypatch)
    producer.prepare_producer_stage(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )
    receipt_path = work / "producer-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    if mutation == "schema-version":
        receipt["schema_version"] = 2
    elif mutation == "artifact":
        receipt["artifact"] = "stock-desk-nsis-repack-producer-receipt-forged"
    elif mutation == "render-inventory":
        receipt["render_inventory"] = list(reversed(receipt["render_inventory"]))
    elif mutation == "original-candidate":
        receipt["original_candidate"]["sha256"] = "0" * 64
    elif mutation == "restored-host":
        receipt["restored_host"]["size"] += 1
    elif mutation == "descriptor":
        receipt["descriptor"]["sha256"] = "0" * 64
    else:
        receipt["unexpected"] = "forged"
    receipt_path.write_bytes(producer.canonical_json_bytes(receipt))

    with pytest.raises(producer.NsisRepackProducerError, match="producer receipt"):
        producer.verify_live_producer_inputs(
            work_root=work, source=source, local_appdata=tmp_path / "local"
        )


def test_verify_live_inputs_rederives_webview_selection_from_rendered_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchors, source, work = _prepare_fixture(tmp_path, monkeypatch)
    producer.prepare_producer_stage(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )
    alternate_relative = (
        "x64/11111111-2222-3333-4444-555555555555/"
        "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    )
    alternate = anchors.tauri_tools_root / alternate_relative
    alternate.parent.mkdir(parents=True)
    alternate.write_bytes(b"forged-but-existing-webview\n")
    forged_snapshot = producer.snapshot_artifacts(
        anchors.tauri_tools_root,
        ("NSIS", alternate_relative),
        tmp_path / "forged-tools-snapshot",
        limits=producer.SnapshotLimits(max_files=1024, max_depth=32),
        allow_windows_hardlinks=False,
        reject_empty_directories=True,
        reject_windows_named_streams=True,
        require_ordinal_paths=True,
    )
    receipt_path = work / "producer-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["webview_relative"] = alternate_relative
    receipt["tools_selection"] = ["NSIS", alternate_relative]
    receipt["tools_snapshot"] = forged_snapshot.summary()
    receipt_path.write_bytes(producer.canonical_json_bytes(receipt))

    with pytest.raises(producer.NsisRepackProducerError, match="WebView|receipt"):
        producer.verify_live_producer_inputs(
            work_root=work, source=source, local_appdata=tmp_path / "local"
        )


def test_verify_live_inputs_rejects_descriptor_change_after_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _anchors_fixture, source, work = _prepare_fixture(tmp_path, monkeypatch)
    result = producer.prepare_producer_stage(
        work_root=work, source=source, local_appdata=tmp_path / "local"
    )
    result.descriptor.write_bytes(b"changed descriptor\n")

    with pytest.raises(producer.NsisRepackProducerError, match="descriptor"):
        producer.verify_live_producer_inputs(
            work_root=work, source=source, local_appdata=tmp_path / "local"
        )


def test_cli_prepare_emits_only_work_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _anchors_fixture, source, work = _prepare_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert (
        producer.main(
            [
                "prepare-stage",
                "--work-root",
                str(work),
                "--event-name",
                source.event_name,
                "--source-ref",
                source.source_ref,
                "--source-sha",
                source.source_sha,
                "--source-tree",
                source.source_tree,
                "--source-epoch",
                str(source.source_epoch),
                "--github-sha",
                source.github_sha,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "descriptor": "descriptor.json",
        "original_candidate": (
            "snapshots/repository/src-tauri/target/x86_64-pc-windows-msvc/"
            "release/bundle/nsis/stock-desk-setup.exe"
        ),
        "producer_receipt": "producer-receipt.json",
        "schema": "stock-desk-nsis-repack-producer-summary-v1",
        "stage": "stage",
    }
