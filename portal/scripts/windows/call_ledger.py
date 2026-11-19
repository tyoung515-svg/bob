#!/usr/bin/env python3
"""Durable dispatch-boundary ledger for the Windows sprint.

Two independent budgets, neither resettable:
  --lane ui     max 12 application live model completions (dispatching the real
                native/relay transport counts, whether it succeeds, fails or is
                unknown)
  --lane graph  max 6 core-DAG canary model calls

Reserve BEFORE the network/transport boundary; resolve after with the observed
outcome. A reservation whose outcome is never resolved stays 'reserved' and is
still spent. Enforces the 07:30+07 cutoff and D/STOP like bob_call.py.
"""
import argparse, datetime, fcntl, json, re, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CUTOFF = datetime.datetime.fromisoformat('2026-10-24T07:30:00+07:00').timestamp()
LANES = {'ui': ('UI-CALLS.json', 12), 'graph': ('GRAPH-CALLS.json', 6)}
STATES = {'achieved', 'failed', 'unknown', 'denied_refused'}


def _locked(root, name):
    p = root / name
    lock = (root / 'BUDGET.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX)
    rows = json.loads(p.read_text()) if p.exists() else []
    return p, lock, rows


def reserve(root, lane, ident, note, now=None):
    now = time.time() if now is None else now
    fname, cap = LANES[lane]
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', ident):
        raise ValueError('invalid dispatch id (lowercase, <=64)')
    if now >= CUTOFF or (root / 'STOP').exists():
        raise ValueError('sprint stopped or past 07:30 dispatch cutoff')
    p, lock, rows = _locked(root, fname)
    try:
        if any(x['id'] == ident for x in rows):
            raise ValueError('id already reserved; inspect the receipt, never replay')
        spent = len(rows)
        if spent >= cap:
            raise ValueError('%s budget exhausted (%d/%d)' % (lane, spent, cap))
        rows.append({'id': ident, 'lane': lane, 'reserved_at': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
                     'state': 'reserved', 'note': note})
        p.write_text(json.dumps(rows, indent=2))
        return {'reserved': ident, 'spent_after': spent + 1, 'budget': cap}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN); lock.close()


def resolve(root, lane, ident, state, receipt):
    if state not in STATES:
        raise ValueError('state must be one of %s' % sorted(STATES))
    fname, cap = LANES[lane]
    p, lock, rows = _locked(root, fname)
    try:
        for row in rows:
            if row['id'] == ident:
                row['state'] = state
                row['resolved_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                row['receipt'] = receipt
                p.write_text(json.dumps(rows, indent=2))
                return row
        raise ValueError('no reservation named %s' % ident)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN); lock.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['reserve', 'resolve', 'status'])
    ap.add_argument('--lane', choices=sorted(LANES), required=True)
    ap.add_argument('--id'); ap.add_argument('--note', default=''); ap.add_argument('--state')
    ap.add_argument('--receipt', default='')
    a = ap.parse_args()
    if a.action == 'status':
        fname, cap = LANES[a.lane]
        rows = json.loads((ROOT / fname).read_text()) if (ROOT / fname).exists() else []
        print(json.dumps({'lane': a.lane, 'spent': len(rows), 'budget': cap, 'rows': rows}, indent=2))
        return 0
    if not a.id:
        raise SystemExit('--id required')
    if a.action == 'reserve':
        print(json.dumps(reserve(ROOT, a.lane, a.id, a.note)))
    else:
        if not a.state:
            raise SystemExit('--state required')
        print(json.dumps(resolve(ROOT, a.lane, a.id, a.state, a.receipt)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
