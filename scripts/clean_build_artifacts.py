from pathlib import Path
import shutil


def clean_build_artifacts(repo: Path) -> None:
    for relative_path in (Path("dist"), Path("web") / "dist"):
        artifact_path = repo / relative_path
        if artifact_path.is_symlink() or artifact_path.is_file():
            artifact_path.unlink()
        elif artifact_path.is_dir():
            shutil.rmtree(artifact_path)


def main() -> None:
    clean_build_artifacts(Path(__file__).resolve().parent.parent)


if __name__ == "__main__":
    main()
