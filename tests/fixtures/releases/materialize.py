"""Materialize versioned data-directory fixtures from a Git checkout."""

from pathlib import Path
import shutil


def materialize_tagged_fixture(source: Path, destination: Path) -> Path:
    """Copy a fixture and restore private runtime permissions.

    Git preserves only the executable bit, so files generated with mode 0600
    and directories generated with mode 0700 arrive in a clean checkout as
    0644/0755. Restore that transport-lost metadata without changing content
    bound by the fixture manifest.
    """
    shutil.copytree(source, destination)
    destination.chmod(0o700)
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise ValueError("tagged release fixtures cannot contain symlinks")
        path.chmod(0o700 if path.is_dir() else 0o600)
    return destination
