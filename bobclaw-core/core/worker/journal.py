"""Installed journal service. Its state is hidden from the execution namespace.

Only the daemon can use the root-created Unix listener: child seccomp denies
Unix socket creation and stealing daemon descriptors. No arbitrary SQL/path RPC.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import sys
import uuid

from .contract import MAX_OUTPUT, root_json

WIRE_LIMIT = MAX_OUTPUT * 2


class Journal:
    def __init__(self, path, *, create=False):
        self.path = Path(path)
        if not create and not self.path.is_file():
            raise RuntimeError('unknown_history')
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        if create:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1),
                  generation INTEGER NOT NULL, controller TEXT, epoch TEXT);
                INSERT OR IGNORE INTO meta VALUES (1,0,NULL,NULL);
                CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY,
                  digest TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT,
                  ownership TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tombstones (id TEXT PRIMARY KEY, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY,
                  execution_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL);
            ''')
        if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('unknown_history')
        self.db.execute('SELECT * FROM meta WHERE id=1').fetchone()

    def close(self):
        self.db.close()

    def call(self, operation, **args):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = self._call(operation, args)
            self.db.execute('INSERT INTO events(execution_id,kind,detail) VALUES (?,?,?)',
                            (args.get('execution_id'), operation, json.dumps(args)))
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _call(self, op, a):
        generation, controller, epoch = self.db.execute('SELECT generation,controller,epoch FROM meta').fetchone()
        if op == 'boot':
            epoch = uuid.uuid4().hex
            # systemd has completed KillMode=control-group before a new activation.
            for execution_id, digest, ownership in self.db.execute("SELECT id,digest,ownership FROM executions WHERE state != 'terminal'").fetchall():
                owned = json.loads(ownership)
                receipt = dict(type='result', execution_id=execution_id, request_digest=digest,
                               outcome='unknown', cleanup='idle_proved', daemon_epoch=epoch,
                               lane_generation=owned['lane_generation'], controller_session=owned['controller_session'],
                               reason='daemon_restart_before_terminal_persistence')
                self.db.execute("UPDATE executions SET state='terminal',receipt=? WHERE id=?", (json.dumps(receipt), execution_id))
            self.db.execute('UPDATE meta SET epoch=?', (epoch,))
        elif op == 'fence':
            incoming = a['lane_generation']
            if incoming < generation or (incoming == generation and a['controller_session'] != controller):
                raise ValueError('stale_controller')
            self.db.execute('UPDATE meta SET generation=?,controller=?', (incoming, a['controller_session']))
            generation, controller = incoming, a['controller_session']
        elif op == 'admit':
            if (a['lane_generation'], a['controller_session'], a['daemon_epoch']) != (generation, controller, epoch):
                raise ValueError('stale_controller')
            row = self.db.execute('SELECT digest,state,receipt FROM executions WHERE id=?', (a['execution_id'],)).fetchone()
            tombstone = self.db.execute('SELECT digest FROM tombstones WHERE id=?', (a['execution_id'],)).fetchone()
            if tombstone and tombstone[0] != a['request_digest']:
                raise ValueError('request_digest_conflict')
            if bool(row) != bool(tombstone):
                return dict(state='unknown_history')
            if row:
                if row[0] != a['request_digest']:
                    raise ValueError('request_digest_conflict')
                return dict(state=row[1], receipt=json.loads(row[2]) if row[2] else None)
            if self.db.execute("SELECT 1 FROM executions WHERE state != 'terminal'").fetchone():
                raise ValueError('lane_busy')
            self.db.execute("INSERT INTO executions VALUES (?,?,'admitted',NULL,?)", (a['execution_id'], a['request_digest'],
                            json.dumps({k: a[k] for k in ('lane_generation', 'controller_session', 'daemon_epoch')})))
            self.db.execute('INSERT INTO tombstones VALUES (?,?)', (a['execution_id'], a['request_digest']))
            return dict(state='new')
        elif op in {'running', 'cleanup', 'terminal'}:
            row = self.db.execute('SELECT state FROM executions WHERE id=?', (a['execution_id'],)).fetchone()
            if not row or row[0] == 'terminal':
                raise ValueError('unknown_history_or_terminal')
            self.db.execute('UPDATE executions SET state=?,receipt=? WHERE id=?',
                            (op, json.dumps(a['receipt']) if op == 'terminal' else None, a['execution_id']))
        elif op == 'inspect':
            row = self.db.execute('SELECT digest,state,receipt FROM executions WHERE id=?', (a['execution_id'],)).fetchone()
            tombstone = self.db.execute('SELECT digest FROM tombstones WHERE id=?', (a['execution_id'],)).fetchone()
            if not row or not tombstone or row[0] != tombstone[0]:
                return dict(state='unknown_history', history_known=bool(row or tombstone),
                            request_digest=tombstone[0] if tombstone else row[0] if row else None)
            return dict(state=row[1], request_digest=row[0], receipt=json.loads(row[2]) if row[2] else None)
        elif op != 'hello':
            raise ValueError('invalid journal operation')
        active = self.db.execute("SELECT id FROM executions WHERE state != 'terminal'").fetchall()
        return dict(daemon_epoch=epoch, lane_generation=generation, controller_session=controller,
                    active_execution=active[0][0] if active else None)


class RemoteJournal:
    def __init__(self, config):
        self.config = config

    def call(self, operation, **args):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(10)
            conn.connect(self.config['journal_socket'])
            _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != 0:
                raise RuntimeError('journal listener identity mismatch')
            conn.sendall(json.dumps(dict(operation=operation, args=args)).encode() + b'\n')
            with conn.makefile('rb') as source:
                line = source.readline(WIRE_LIMIT + 1)
            if not line.endswith(b'\n') or len(line) > WIRE_LIMIT:
                raise RuntimeError('journal unavailable')
            result = json.loads(line)
            if 'error' in result:
                raise ValueError(result['error'])
            return result


def main():
    import ctypes
    # Same UID is insufficient protection against /proc/PID/{root,fd} aliases
    # into the helper's different mount namespace. Deny ptrace-style access.
    if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0):  # PR_SET_DUMPABLE
        raise RuntimeError('journal proc isolation unavailable')
    cfg = root_json(Path(sys.argv[1]))
    if os.getuid() != cfg['worker_uid'] or os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise RuntimeError('journal requires installed socket activation')
    journal = Journal(cfg['journal_path'])  # Never recreate missing history.
    with socket.socket(fileno=3) as listener:
        while True:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(10)
                _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != cfg['worker_uid']:
                    continue
                try:
                    with conn.makefile('rb') as source:
                        line = source.readline(WIRE_LIMIT + 1)
                    if not line.endswith(b'\n') or len(line) > WIRE_LIMIT:
                        raise ValueError('invalid journal request')
                    value = json.loads(line)
                    result = journal.call(value['operation'], **value['args'])
                except (ValueError, KeyError, sqlite3.Error):
                    result = {'error': 'journal refused; unknown_history'}
                conn.sendall(json.dumps(result).encode() + b'\n')


if __name__ == '__main__':
    main()
