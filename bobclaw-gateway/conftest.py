"""
Root conftest.py — executed by pytest before any test module is imported.

Sets required environment variables for the test suite so that config.py
loads valid values even when no .secrets/bobclaw.env file is present, and
isolates the gateway SQLite DB per test inside a repo-local, gitignored
directory (test-db/).

No pytest basetemp / temp-root relocation is used (an earlier audit superseded
that approach as brittle on Windows / sandboxed runners). Each test gets its
own unique DB file (sanitised nodeid + run-unique identifier) under test-db/.
No files are ever deleted — stale residue accumulates harmlessly in the
gitignored directory, avoiding PermissionError [WinError 5] from retained
SQLite / AV / indexing handles on Windows.
"""
import asyncio
import os
import re
import sys
import uuid
from pathlib import Path

# ── Set test env vars BEFORE any application module is imported ─────────────
# os.environ.setdefault only writes if the key is absent, so real env values
# (e.g. from CI secrets) always take precedence.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-key-32-chars-minimum!!")
os.environ.setdefault("BOBCLAW_PASSWORD", "testpassword123")
os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")  # valid base32, 16 chars; self-contained (was a non-base32 placeholder only ever masked by the .secrets override)
os.environ.setdefault("POSTGRES_URL", "postgresql://test:test@localhost:5432/test")

_GATEWAY_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

# Repo-local, gitignored directory holding every test SQLite file. Created up
# front so the parent dir always exists before any SQLite open — including the
# server's on-startup init_db (unittest setUpClass), which runs before the
# per-test fixture below.
_TEST_DB_DIR = _GATEWAY_DIR / "test-db"
_TEST_DB_DIR.mkdir(exist_ok=True)

# Run-unique identifier so every pytest invocation gets fresh DB paths without
# needing to delete stale files (which can fail on Windows when SQLite / AV /
# indexing retains handles).  Residue from prior runs accumulates in test-db/
# harmlessly — the directory is gitignored.
_RUN_ID = uuid.uuid4().hex

# In-memory rollback journal: no -journal/-wal sidecars are ever written.
os.environ["GATEWAY_SQLITE_JOURNAL_MODE"] = "MEMORY"
# Bootstrap default (used only until the per-test fixture overrides it) so the
# setUpClass startup init lands in test-db/ and never the committed gateway.db.
os.environ.setdefault("GATEWAY_SQLITE_PATH", str(_TEST_DB_DIR / "gateway-bootstrap.db"))

# ── Ensure bobclaw-gateway/ and bobclaw-core/ are importable ──────────────────
if str(_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_DIR))
_CORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bobclaw-core")
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import pytest


@pytest.fixture(autouse=True)
def _isolated_gateway_db(request):
    """Give every test its own freshly-initialised SQLite DB under test-db/.

    db.py reads GATEWAY_SQLITE_PATH dynamically on each connection, so pointing
    the env var at a unique per-test file fully isolates refresh-token (and any
    other SQLite) state between tests. The path includes a run-unique identifier
    (``_RUN_ID``) so different pytest invocations never collide, and a
    sanitised nodeid so failures are easy to correlate with a test name.

    No files are deleted at startup or teardown — stale residue accumulates
    harmlessly in the gitignored test-db/ directory. This avoids PermissionError
    [WinError 5] on Windows when SQLite / AV / indexing retains handles.
    The committed gateway.db is never touched.
    """
    from db import init_db

    safe = re.sub(r"[^A-Za-z0-9]+", "_", request.node.nodeid).strip("_")
    db_path = _TEST_DB_DIR / f"{safe}_{_RUN_ID}.db"
    os.environ["GATEWAY_SQLITE_PATH"] = str(db_path)
    asyncio.run(init_db())
    yield
