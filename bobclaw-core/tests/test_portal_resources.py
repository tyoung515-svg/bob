"""Offline domain checks using actual SQLite records and filesystem opens."""
import base64
import json
import os
from pathlib import Path
import sqlite3
import uuid

import pytest

from core.portal_resources import (ResourceError, Snapshots, browse_file,
                                   owned_memory, register_project_folder)


@pytest.fixture(autouse=True)
def mock_redis():
    yield  # These domain operations never load model/graph/Redis code.


@pytest.fixture
def memory(tmp_path):
    owner = 'orchid-7'
    for name in (owner, 'shared', 'foreign-9'):
        (tmp_path / name).mkdir()
    (tmp_path / owner / 'MEMORY.md').write_text('你好\n\n!!!\n\n' + '\n\n'.join(f'note {i}' for i in range(65)), encoding='utf-8')
    (tmp_path / 'foreign-9' / 'MEMORY.md').write_text('foreign secret')
    db = tmp_path / 'memory.db'
    with sqlite3.connect(db) as conn:
        conn.executescript('CREATE TABLE memory_events(event_id TEXT, body_json TEXT, ts TEXT, insertion_order INTEGER); CREATE TABLE memory_facts(fact_id TEXT, body_json TEXT, source_event_id TEXT, ts TEXT);')
        for i, who in enumerate((owner, 'foreign-9', None)):
            conn.execute('INSERT INTO memory_events VALUES(?,?,?,?)', (str(i), json.dumps({'user_id': who, 'user_message': 'owned'}), 'now', i))
            conn.execute('INSERT INTO memory_facts VALUES(?,?,?,?)', (str(i), 'fact content', str(i), 'now'))
    scope = {'may_touch': [str(tmp_path / owner) + '/**', str(tmp_path / 'shared') + '/**']}
    return owner, db, scope


def test_arbitrary_owner_complete_stable_snapshot(tmp_path, memory):
    owner, db, scope = memory
    rows, coverage = owned_memory(tmp_path, db, owner, scope)
    assert len(rows) == 69
    assert {r['owner'] for r in rows} == {owner}
    assert {'你好', '!!!'} <= {r['content'] for r in rows}
    assert coverage['counts_by_kind'] == {'family_memory': 67, 'bob_event': 1, 'bob_fact': 1}
    assert coverage['unattributed_events_excluded'] == 1
    store = Snapshots()
    first = store.page('owner/filter/scope/lane', rows, coverage, limit=50)
    with (tmp_path / owner / 'MEMORY.md').open('a') as stream:
        stream.write('\n\nnew append')
    second = store.page('owner/filter/scope/lane', cursor=first['next_cursor'], limit=50)
    assert first['snapshot'] == second['snapshot']
    assert second['next_cursor'] is None
    assert first['items'] + second['items'] == rows
    assert len(owned_memory(tmp_path, db, owner, scope)[0]) == 70
    with pytest.raises(ResourceError, match='invalid_cursor'):
        store.page('foreign binding', cursor=first['next_cursor'])


@pytest.mark.skipif(os.geteuid() == 0, reason='root reads any file')
def test_lane_database_unreadable_by_this_account_is_coverage_not_a_fault(tmp_path, memory):
    owner, db, scope = memory
    db.chmod(0o000)  # the runtime gateway account cannot read the core-owned lane database
    try:
        rows, coverage = owned_memory(tmp_path, db, owner, scope)
    finally:
        db.chmod(0o600)
    assert {r['kind'] for r in rows} == {'family_memory'} and len(rows) == 67
    assert coverage['lane_database'] == 'unavailable_or_out_of_scope'
    assert coverage['counts_by_kind'] == {'family_memory': 67, 'bob_event': 0, 'bob_fact': 0}


def test_corrupt_lane_database_is_still_a_coded_fault(tmp_path, memory):
    owner, db, scope = memory
    db.write_bytes(b'not a database')
    with pytest.raises(ResourceError, match='memory_unavailable') as caught:
        owned_memory(tmp_path, db, owner, scope)
    assert caught.value.status == 503


def test_snapshot_expiry():
    now = [1]
    store = Snapshots(ttl=3, clock=lambda: now[0])
    page = store.page('a', [1, 2], {}, limit=1)
    now[0] = 5
    with pytest.raises(ResourceError, match='snapshot_expired'):
        store.page('a', cursor=page['next_cursor'])


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / 'projects' / 'sample'
    root.mkdir(parents=True)
    (root / 'hello.txt').write_bytes(b'abcdefghij')
    (root / 'binary.bin').write_bytes(b'\x00\xff\x80')
    (root / '.env').write_text('secret')
    row = register_project_folder(tmp_path, 'orchid-7', str(uuid.uuid4()), 'projects/sample', 'Sample')
    scope = {'may_touch': [str(root) + '/**', str(root)]}
    return root, row, scope


@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', 'C:/Windows', 'C:relative', '\\\\host\\share', 'a\\..\\b', '%2e%2e/x', '%252e%252e/x', 'hello.txt\x00', 'a//b', './hello.txt', '\ud800', 'hello\x7f'])
def test_paths_rejected(tmp_path, folder, path):
    _, row, scope = folder
    with pytest.raises(ResourceError):
        browse_file(tmp_path, row, scope, path)


def test_bounded_read_binary_listing(tmp_path, folder):
    _, row, scope = folder
    first = browse_file(tmp_path, row, scope, 'hello.txt', max_bytes=4)
    assert first['content'] == 'abcd' and first['truncated'] and first['next_offset'] == 4
    second = browse_file(tmp_path, row, scope, 'hello.txt', offset=4)
    assert second['content'] == 'efghij' and not second['truncated'] and second['next_offset'] is None
    assert first['revision'] == second['revision']
    binary = browse_file(tmp_path, row, scope, 'binary.bin')
    assert binary['encoding'] == 'base64'
    assert base64.b64decode(binary['content']) == b'\x00\xff\x80'
    assert {r['name'] for r in browse_file(tmp_path, row, scope, listing=True)} == {'hello.txt', 'binary.bin'}
    with pytest.raises(ResourceError, match='scope_denied'):
        browse_file(tmp_path, row, scope, '.env')
    for size in (0, 65537, True):
        with pytest.raises(ResourceError, match='invalid_args'):
            browse_file(tmp_path, row, scope, 'hello.txt', max_bytes=size)


def test_oversize_file(tmp_path, folder):
    root, row, scope = folder
    with (root / 'big').open('wb') as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(ResourceError, match='file_too_large'):
        browse_file(tmp_path, row, scope, 'big')


def test_symlink_escape(tmp_path, folder):
    root, row, scope = folder
    outside = tmp_path / 'outside'
    outside.write_text('foreign secret')
    try:
        (root / 'escape').symlink_to(outside)
        (root / 'linked-dir').symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('Windows symlink privileges unavailable')
    for path in ('escape', 'linked-dir/outside'):
        with pytest.raises(ResourceError, match='not_found|conflict'):
            browse_file(tmp_path, row, scope, path)


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO test')
def test_fifo_rejected_without_blocking(tmp_path, folder):
    root, row, scope = folder
    os.mkfifo(root / 'pipe')
    with pytest.raises(ResourceError, match='not_found'):
        browse_file(tmp_path, row, scope, 'pipe')


def test_registration_rejects_control_roots(tmp_path):
    with pytest.raises(ResourceError):
        register_project_folder(tmp_path, 'orchid-7', str(uuid.uuid4()), 'cli/claude', 'Credentials')


def test_memory_database_uses_lane_configuration(tmp_path, monkeypatch):
    from core.paths import memory_sqlite_path
    monkeypatch.delenv('MEMORY_SQLITE_PATH', raising=False)
    monkeypatch.setenv('BOB_LANE_ROOT', str(tmp_path / 'lane-93'))
    assert memory_sqlite_path() == tmp_path / 'lane-93' / 'bobclaw_memory.db'
