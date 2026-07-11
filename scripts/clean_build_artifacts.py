from pathlib import Path
import shutil


def clean_build_artifacts(repo: Path) -> None:
    for relative_path in (
        Path("build"),
        Path("dist"),
        Path("web") / "dist",
        Path("src-tauri") / "target",
    ):
        artifact_path = repo / relative_path
        if artifact_path.is_symlink() or artifact_path.is_file():
            artifact_path.unlink()
        elif artifact_path.is_dir():
            shutil.rmtree(artifact_path)
    binaries = repo / "src-tauri" / "binaries"
    if binaries.is_dir() and not binaries.is_symlink():
        for executable in binaries.glob("*.exe"):
            if executable.is_file() or executable.is_symlink():
                executable.unlink()


def main() -> None:
    clean_build_artifacts(Path(__file__).resolve().parent.parent)


if __name__ == "__main__":
    main()
