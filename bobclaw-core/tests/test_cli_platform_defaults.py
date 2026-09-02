"""Platform-aware CLI backend config defaults (spawn-context intake, bug 2).

The CLI scratch/home defaults were Windows literals (``C:/dev/scratch/...``,
an AppData agy path). On POSIX those resolve as RELATIVE paths and spray
``C:/`` directories under the service cwd. ``_default_cli_scratch_root`` is the
pure helper the config class now derives them from — test it directly (the
frozen ``config`` attribute values depend on the host env, the helper does not).
"""
import os

from core.config import _default_cli_scratch_root


def test_scratch_root_is_absolute():
    root = _default_cli_scratch_root("cc")
    if os.name == "nt":
        assert root == "C:/dev/scratch/cc"
    else:
        assert os.path.isabs(root), "POSIX default must be absolute, not C:/-relative"
        assert not root.startswith("C:")
        assert root.endswith(os.path.join(".bobclaw", "scratch", "cc"))


def test_scratch_root_varies_by_backend_name():
    assert _default_cli_scratch_root("cc") != _default_cli_scratch_root("agy")
    assert _default_cli_scratch_root("agy-home").endswith("agy-home")
