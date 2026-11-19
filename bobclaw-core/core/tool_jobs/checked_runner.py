"""Installed offline adapter. Invoked only inside the common worker boundary.

Project code runs here, never in core. The daemon owns deadlines and descendant
cleanup, including grandchildren of build/check subprocesses.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from core.execution.checked import edits, path, protected, allowed_edit
from core.execution.models import ExecutionError, canonical, digest
from core.execution.planning import strict_json
from core.execution.workspaces import snapshot
from core.portal_resources import safe_open, relative_parts, ResourceError


JOURNAL = '.bob-checked-edit.json'


def read_journal(root):
    try:
        (Path(root) / JOURNAL).lstat()
    except FileNotFoundError:
        return None
    try:
        with safe_open(Path(root), (JOURNAL,)) as (fd, _, info):
            if info.st_size > 800000:
                raise ExecutionError('edit_journal_invalid', 409)
            with os.fdopen(os.dup(fd), 'r', encoding='utf-8') as stream:
                prior = strict_json(stream.read(800001))
    except FileNotFoundError:
        return None
    except (ValueError, TypeError, UnicodeError, ResourceError) as exc:
        raise ExecutionError('edit_journal_invalid', 409) from exc
    if (not isinstance(prior, dict) or not isinstance(prior.get('operation_key'), str)
            or type(prior.get('completed')) is not bool
            or not isinstance(prior.get('base'), dict)
            or not isinstance(prior['base'].get('manifest'), list)):
        raise ExecutionError('edit_journal_invalid', 409)
    return prior


def journal_blocker(root, operation_key=None):
    prior = read_journal(root)
    if prior and not prior['completed'] and prior['operation_key'] != operation_key:
        return {'operation_key': prior['operation_key'],
                'recovery': "Retry the blocking job's publication with its original request key to reconcile the journal."}
    return None


def edit_progress(base, current, value):
    """Every changed byte must be an exact declared preimage or postimage."""
    before = {i['path']:i for i in base['manifest']}
    actual = {i['path']:i for i in current['manifest']}
    changes = {i['path']:i for i in value['edits']}
    parents = {'/'.join(i['path'].split('/')[:n]) for i in value['edits'] if i['operation']=='create'
               for n in range(1,len(i['path'].split('/')))}
    pending = []
    for name in before.keys() | actual.keys() | changes.keys():
        old, now = before.get(name), actual.get(name)
        item = changes.get(name)
        if item is None:
            if old is None and name in parents and now == {'path':name,'kind':'directory','mode':0o755}:
                continue
            if old != now:
                raise ExecutionError('edit_reconciliation_conflict', 409)
            continue
        if (old.get('sha256') if old else None) != item['pre_sha256']:
            raise ExecutionError('stale_preimage', 409)
        if old == now:
            if item['operation']=='replace' and old and hashlib.sha256(item['content'].encode()).hexdigest()==old['sha256']:
                continue
            pending.append(item)
            continue
        if item['operation'] == 'delete' and now is None:
            continue
        content = item.get('content', '').encode('utf-8')
        expected = {'path':name,'kind':'file','sha256':hashlib.sha256(content).hexdigest(),
                    'size':len(content),'mode':old['mode'] if old else 0o644}
        if now != expected:
            raise ExecutionError('edit_reconciliation_conflict', 409)
    return pending


def ensure_parents(root, parts):
    for index, part in enumerate(parts):
        with safe_open(Path(root), parts[:index], directory=True) as (dfd, parent, _):
            try:
                if dfd is None: (parent/part).mkdir(mode=0o770)
                else: os.mkdir(part,mode=0o770,dir_fd=dfd)
            except FileExistsError:
                pass
            if dfd is not None: os.fsync(dfd)
        # Existing or newly-created links/files are refused by the same FD walk.
        with safe_open(Path(root), parts[:index+1], directory=True):
            pass


def write_journal(root, state):
    # Exclusive temporary + fsync + replace. A crash leaves either the original
    # durable intent or the completed one, never an authority inferred from prose.
    temporary = Path(root) / ('bob-journal-' + uuid.uuid4().hex)
    try:
        with temporary.open('x',encoding='utf-8',newline='\n') as stream:
            stream.write(canonical(state));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,Path(root)/JOURNAL)
        if os.name != 'nt':
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def apply_edits(root, value, write_paths, *, operation_key=None, input_digest=None, allow_new_tests=False):
    # Lock the directory inode, not a replaceable lock-file. Serializes managed
    # publications and their journal reconciliation on the installed Linux worker.
    with safe_open(Path(root), (), directory=True) as (fd, _, _):
        if fd is not None:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        return _apply_edits(root, value, write_paths, operation_key=operation_key,
                            input_digest=input_digest, allow_new_tests=allow_new_tests)


def _apply_edits(root, value, write_paths, *, operation_key=None, input_digest=None, allow_new_tests=False):
    edits(value)
    frozen = snapshot(root)
    files = {x['path']: x for x in frozen['manifest'] if x['kind'] == 'file'}
    prior = read_journal(root) if operation_key else None
    same = prior and prior.get('operation_key') == operation_key
    if same:
        if prior.get('intent_digest') != digest(value) or prior.get('input_digest') != input_digest:
            raise ExecutionError('edit_journal_conflict',409)
        items = edit_progress(prior['base'],frozen,value)
    else:
        if prior and not prior.get('completed'):
            raise ExecutionError('edit_reconciliation_required',409,journal_blocker(root,operation_key))
        if input_digest and frozen['digest'] != input_digest:
            raise ExecutionError('stale_snapshot',409)
        items = value['edits']
    # Validate EVERY preimage before the first write. A stale batch writes nothing.
    for item in value['edits']:
        if not allowed_edit(item, write_paths, allow_new_tests):
            raise ExecutionError('edit_scope_denied', 403)
    for item in items:
        name = item['path']
        if not allowed_edit(item, write_paths, allow_new_tests):
            raise ExecutionError('edit_scope_denied', 403)
        old = files.get(name)
        if any(p.casefold()==name.casefold() and p!=name for p in files):
            raise ExecutionError('edit_path_alias',409)
        if (old['sha256'] if old else None) != item['pre_sha256']:
            raise ExecutionError('stale_preimage', 409)
        if item['operation'] == 'create' and any(x['path'] == name for x in frozen['manifest']):
            raise ExecutionError('stale_preimage', 409)
    if operation_key:
        state = prior if same else {'operation_key':operation_key,'input_digest':input_digest,'intent_digest':digest(value),
                                    'base':{'manifest':frozen['manifest']},'completed':False}
        write_journal(root,state)
    for item in items:
        parts = relative_parts(item['path'])
        if item['operation']=='create':
            ensure_parents(root,parts[:-1])
        # A deterministic adapter is the only writer to this workspace. Parent
        # walks never follow links, including parents created for new files.
        with safe_open(Path(root), parts[:-1], directory=True) as (dfd, parent, _):
            target = parent / parts[-1]
            if item['operation'] == 'delete':
                if dfd is None:
                    target.unlink()
                else:
                    os.unlink(parts[-1], dir_fd=dfd)
            else:
                temporary = 'bob-edit-' + uuid.uuid4().hex
                data = item['content'].encode('utf-8')
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
                mode = 0o770 if files.get(item['path'],{}).get('mode',0o644) & 0o111 else 0o660
                fd = os.open(temporary if dfd is not None else parent / temporary, flags, mode, **({'dir_fd': dfd} if dfd is not None else {}))
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if dfd is None:
                        os.replace(parent / temporary, target)
                    else:
                        os.replace(temporary, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=dfd) if dfd is not None else (parent / temporary).unlink()
                    except FileNotFoundError:
                        pass
            if dfd is not None:
                os.fsync(dfd)
    # Use the same snapshot function/exclusions as frozen checked output. This
    # return is computed before apply_edits releases the directory inode lock.
    output_digest = snapshot(root)['digest']
    if operation_key:
        write_journal(root,{**state,'completed':True,'output_digest':output_digest})
    return {'edited': [i['path'] for i in value['edits']], 'resumed':bool(same),
            'output_digest':output_digest}


def check_command(adapter):
    # Compatible with core.build.skeleton's minikit contract. No caller argv.
    from core.execution.checked import CHECKS
    return [sys.executable,*CHECKS['python.unittest' if adapter == 'python.baseline' else adapter]['arguments']]


def execute(value, root=Path('.')):
    if set(value) - {'operation', 'input_digest', 'paths', 'edits', 'write_paths', 'build_contracts', 'operation_key', 'allow_new_tests', 'test_evidence'}:
        raise ExecutionError('unsupported_fields')
    frozen = snapshot(root)
    if frozen['digest'] != value['input_digest'] and value['operation'] != 'workspace.edit':
        raise ExecutionError('stale_snapshot', 409)
    op = value['operation']
    if op == 'workspace.read':
        files = {x['path']: x for x in frozen['manifest'] if x['kind'] == 'file'}
        result, remaining = [], 48000
        for name in value['paths']:
            path(name)
            item = files.get(name)
            if not item:
                raise ExecutionError('file_unavailable', 404)
            data = frozen['blobs'][item['sha256']]
            if len(data) > remaining:
                raise ExecutionError('read_too_large', 413)
            content = data.decode('utf-8')
            remaining -= len(data)
            result.append({'path': name, 'sha256': item['sha256'], 'content': content})
        return {'files': result}
    if op == 'workspace.edit':
        return apply_edits(root, {'edits': value['edits']}, value['write_paths'],
                           operation_key=value.get('operation_key'),input_digest=value['input_digest'],allow_new_tests=value.get('allow_new_tests') is True)
    if op == 'python.scaffold':
        from core.build.skeleton import write_app
        # The helper is pure deterministic generation in this worker, never a
        # nested worker acquisition. Existing scaffold paths are not overwritten.
        if any((root / p).exists() for p in ('minikit', 'tests')):
            raise ExecutionError('scaffold_paths_exist', 409)
        write_app(root, value['build_contracts'], {}, worker_local=True)
        return {'scaffold': 'minikit', 'contracts': len(value['build_contracts'])}
    if op == 'files.nonempty':
        if not any(i['kind'] == 'file' and i['size'] > 0 for i in frozen['manifest']):
            raise ExecutionError('check_failed')
        return {'assertions': ['snapshot_has_nonempty_file']}
    if op in {'python.import', 'python.collect', 'python.pytest', 'python.cli', 'python.compile', 'python.unittest', 'python.baseline'}:
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
        # Forward the combined stream to the daemon, which kills on overflow.
        # Retain only a bounded summary tail; cancellation owns no host thread.
        process = subprocess.Popen(check_command(op), cwd=root, env=env, stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        tail = bytearray()
        collect_evidence = op == 'python.baseline' or op == 'python.unittest' and value.get('test_evidence') is True
        evidence_output = bytearray()
        evidence_overflow = False
        while chunk := process.stdout.read(4096):
            sys.stdout.buffer.write(chunk);sys.stdout.buffer.flush()
            tail.extend(chunk)
            if len(tail)>65536: del tail[:-65536]
            if collect_evidence:
                if len(evidence_output) + len(chunk) <= 262144 and not evidence_overflow:
                    evidence_output.extend(chunk)
                else:
                    evidence_overflow = True
        status = process.wait()
        summary = tail.decode('utf-8','replace')
        if op in {'python.unittest', 'python.baseline'}:
            import re
            found = re.search(r'Ran (\d+) tests?\b', summary)
            if not found or int(found[1]) < 1: raise ExecutionError('no_tests_executed')
            if collect_evidence:
                from core.execution.checked import unittest_evidence
                if evidence_overflow: raise ExecutionError('test_evidence_limit',409)
                evidence = unittest_evidence(evidence_output.decode('utf-8','replace'))
            if op == 'python.baseline':
                return {'baseline': True, 'exit_status': status, **evidence,
                        'passed': status == 0, 'baseline_already_passing': status == 0,
                        'output_tail': summary[-8000:]}
        if status != 0:
            raise ExecutionError('check_failed')
        if op == 'python.collect':
            import re
            found = re.search(r'(\d+) tests? collected',summary)
            if not found or int(found[1])<1: raise ExecutionError('check_failed')
        if op == 'python.pytest':
            from core.build.skeleton import parse_pytest_summary
            parsed = parse_pytest_summary(summary)
            if parsed['passed']<1 or parsed['failed'] or parsed['errors']: raise ExecutionError('check_failed')
        return {'assertions': [op], 'exit_status': status,
                **(evidence if op == 'python.unittest' and collect_evidence else {})}
    raise ExecutionError('unregistered_operation')


def main():
    # Run copies are setgid to the worker group, of which core is a member.
    # The daemon's private-home umask is 077; artifacts need explicit group read
    # so the service can freeze them after cleanup. Landlock still confines IO.
    os.umask(0o007)
    try:
        raw = sys.stdin.buffer.read(1000001)
        if len(raw) > 1000000:
            raise ExecutionError('adapter_input_too_large')
        result = execute(strict_json(raw.decode('utf-8')))
        print(canonical({'adapter': 'checked-edits/1', 'result': result}))
    except (ExecutionError, ResourceError, ValueError, OSError) as exc:
        print(canonical({'adapter': 'checked-edits/1', 'error': getattr(exc, 'code', type(exc).__name__)}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
