"""Offline packaging contract and executable fail-stop checks; no image build."""
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

RUNTIME = Path(__file__).resolve().parents[1]


def test_fetch_checked_is_installed_root_executable_in_final_stage():
    final = (RUNTIME / 'Containerfile').read_text().rsplit('FROM ubuntu:24.04', 1)[1]
    assert 'install -o root -g root -m 0755 /opt/bob/deploy/runtime/fetch-checked.sh /usr/local/bin/fetch-checked' in final
    assert "command -v fetch-checked" in (RUNTIME / 'smoke.sh').read_text()



# bob-embedder is now the Python CLI in lib/embedder.py (S4); its fetch failure handling
# (non-zero fetch-checked exit or SHA mismatch never installs) is covered by test_embedder_choice.py.
