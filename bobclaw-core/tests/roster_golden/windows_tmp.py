"""Optional pytest runner shim for Windows 3.13 under the restricted PC token.

Python 3.13 gives mkdir(mode=0700) a special Windows DACL that excludes the
sandbox token. Keep inherited ACLs for newly-created test scratch directories.
This changes no existing permissions, production paths, or application code.
Load explicitly with -p roster_golden.windows_tmp and core/tests on PYTHONPATH.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_mkdir = os.mkdir


def pytest_configure(config):
    if os.name != "nt":
        return

    def mkdir(path, mode=0o777, *, dir_fd=None):
        selected = Path(path).absolute()
        if mode == 0o700 and selected.is_relative_to(ROOT) and any(
            part in {".test-tmp", "test-db"} for part in selected.parts
        ):
            mode = 0o777
        return _mkdir(path, mode, dir_fd=dir_fd)

    os.mkdir = mkdir


def pytest_unconfigure(config):
    os.mkdir = _mkdir
