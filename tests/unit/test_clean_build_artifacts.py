from __future__ import annotations

from pathlib import Path

from scripts import clean_build_artifacts as cleaner


def test_clean_build_artifacts_removes_directories_files_and_symlinks(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "package.whl").write_bytes(b"wheel")
    web = tmp_path / "web"
    web.mkdir()
    web_dist = web / "dist"
    web_dist.write_bytes(b"not-a-directory")

    cleaner.clean_build_artifacts(tmp_path)

    assert not dist.exists()
    assert not web_dist.exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "preserved.txt").write_text("keep", encoding="utf-8")
    dist.symlink_to(outside, target_is_directory=True)

    cleaner.clean_build_artifacts(tmp_path)

    assert not dist.exists()
    assert (outside / "preserved.txt").read_text(encoding="utf-8") == "keep"


def test_clean_build_artifacts_is_idempotent_and_main_uses_repo_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cleaner.clean_build_artifacts(tmp_path)
    observed: list[Path] = []
    monkeypatch.setattr(cleaner, "clean_build_artifacts", observed.append)

    cleaner.main()

    assert observed == [Path(cleaner.__file__).resolve().parent.parent]
