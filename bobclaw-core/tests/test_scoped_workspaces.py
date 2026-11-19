"""Real snapshots, immutable digest binding, and Linux child confinement."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.execution.models import ExecutionError, digest
from core.execution.workspaces import snapshot, materialize, proposal
from core.worker import landlock
from core.worker.contract import WorkerUnavailable, resolve_profile


@pytest.mark.parametrize('case', ['missing_run', 'invalid_run', 'missing_root',
                                 'unknown_workspace', 'missing_cwd', 'file_cwd', 'escape_cwd'])
def test_profile_resolution_failures_are_worker_refusals(tmp_path, case):
    root = tmp_path / 'runs'; root.mkdir()
    selected = root / ('a' * 32); selected.mkdir()
    (selected / 'file').write_text('not a directory')
    cfg = {'service_uid': selected.stat().st_uid, 'run_workspace_root': str(root),
           'workspaces': {'scratch': str(selected)},
           'profiles': {'python': {'argv': ['/usr/bin/python3']}}}
    request = dict(profile='python', workspace='run_' + 'a' * 32, cwd='', args=[])
    if case == 'missing_run': request['workspace'] = 'run_' + 'b' * 32
    elif case == 'invalid_run': request['workspace'] = 'run_invalid'
    elif case == 'missing_root': cfg['run_workspace_root'] = str(tmp_path / 'absent')
    elif case == 'unknown_workspace': request['workspace'] = 'foreign'
    elif case == 'missing_cwd': request['cwd'] = 'absent'
    elif case == 'file_cwd': request['cwd'] = 'file'
    else: request['cwd'] = '..'
    with pytest.raises(WorkerUnavailable):
        resolve_profile(cfg, request)


@pytest.mark.skipif(not hasattr(os, 'O_PATH'), reason='Linux search-only dirfds')
def test_run_workspace_traversal_needs_no_parent_listing_and_keeps_nofollow(tmp_path, monkeypatch):
    from core.portal_resources import safe_open, ResourceError
    root = tmp_path / 'runs'; root.mkdir(mode=0o2710)
    selected = root / ('a' * 32); selected.mkdir()
    native, opened = os.open, []
    def checked_open(path, flags, *args, **kwargs):
        opened.append((path, flags))
        if Path(path) == root and not flags & os.O_PATH:
            # Deterministic even under root, which otherwise masks EACCES.
            raise PermissionError('run root is search-only for the worker')
        return native(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', checked_open)
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {checked_open})
    with safe_open(root, (selected.name,), directory=True) as (fd, _, info):
        assert info.st_ino == selected.stat().st_ino
        assert os.listdir(fd) == []  # final fd remains readable
    assert opened[0][1] & os.O_PATH
    assert not opened[1][1] & os.O_PATH
    assert all(flags & os.O_NOFOLLOW for _, flags in opened)
    alias = root / ('b' * 32); alias.symlink_to(selected, target_is_directory=True)
    with pytest.raises(ResourceError):
        with safe_open(root, (alias.name,), directory=True): pass
    ancestor_alias = tmp_path / 'alias'; ancestor_alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ResourceError):
        with safe_open(ancestor_alias, (selected.name,), directory=True): pass


@pytest.mark.skipif(os.name == 'nt', reason='POSIX run ownership and modes')
@pytest.mark.parametrize('case', ['foreign_owner', 'world_writable'])
def test_run_workspace_protection_is_still_required(tmp_path, case):
    selected = tmp_path / ('a' * 32); selected.mkdir()
    cfg = {'service_uid': os.geteuid() + (case == 'foreign_owner'),
           'run_workspace_root': str(tmp_path), 'profiles': {'python': {'argv': ['/usr/bin/python3']}}}
    if case == 'world_writable': selected.chmod(0o777)
    with pytest.raises(WorkerUnavailable, match='protection unavailable'):
        resolve_profile(cfg, dict(profile='python', workspace='run_' + selected.name, cwd='', args=[]))


def test_snapshot_copy_digest_and_proposal_delete_are_bound(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'a.txt').write_text('original')
    (source/'delete.txt').write_text('old')
    frozen=snapshot(source)
    parent=tmp_path/'runs';parent.mkdir()
    target=materialize(parent,'a'*32,frozen)
    assert snapshot(target)['digest']==frozen['digest']
    (target/'a.txt').write_text('new')
    (target/'delete.txt').unlink()
    (target/'created.txt').write_text('new file')
    out=snapshot(target)
    result=proposal(frozen,out,{'job_id':'job','project_id':'project'})
    assert {r['operation'] for r in result['files']}=={'create','replace','delete'}
    assert next(r for r in result['files'] if r['operation']=='delete')['post_sha256'] is None
    assert result['base_digest']==frozen['digest'] and result['output_digest']==out['digest']
    payload={k:v for k,v in result.items() if k not in {'digest','diff','diff_truncated','check_runs','checks_passed'}}
    assert digest(payload)==result['digest']
    assert (source/'a.txt').read_text()=='original'
    assert snapshot(source)['digest']==frozen['digest']
    (target/'a.txt').write_text('changed again')
    assert proposal(frozen,snapshot(target),{'job_id':'job','project_id':'project'})['digest']!=result['digest']


@pytest.mark.parametrize('kind',['symlink','hardlink','credential','special'])
def test_snapshot_refuses_link_special_and_credential_inputs(tmp_path,kind):
    source=tmp_path/'source';source.mkdir()
    outside=tmp_path/'outside';outside.write_text('private')
    if kind=='symlink':
        try: (source/'link').symlink_to(outside)
        except OSError: pytest.skip('symlink creation unavailable on this host')
    elif kind=='hardlink': os.link(outside,source/'linked')
    elif kind=='credential': (source/'.env').write_text('private')
    else:
        if not hasattr(os,'mkfifo'): pytest.skip('POSIX special files required')
        os.mkfifo(source/'fifo')
    with pytest.raises(ExecutionError): snapshot(source)


def test_snapshot_bounds_and_copy_never_share_inodes(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'a').write_bytes(b'x'*32)
    with pytest.raises(ExecutionError,match='too_large'): snapshot(source,max_bytes=16)
    with pytest.raises(ExecutionError,match='too_large'): snapshot(source,max_files=0)
    parent=tmp_path/'runs';parent.mkdir()
    copy=materialize(parent,'b'*32,snapshot(source))
    assert (source/'a').stat().st_ino!=(copy/'a').stat().st_ino
    assert (source/'a').stat().st_nlink==(copy/'a').stat().st_nlink==1


def test_landlock_rules_include_refer_truncate_and_only_selected_writes(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir()
    temporary=tmp_path/'temporary';temporary.mkdir()
    declared=tmp_path/'readonly';declared.mkdir()
    rules=landlock.rules(workspace,temporary,readonly=[str(declared)],runtime=())
    assert landlock.MIN_ABI==3
    assert landlock.HANDLED & landlock.REFER and landlock.HANDLED & landlock.TRUNCATE
    assert rules[:2]==[(str(workspace.resolve()),landlock.HANDLED),(str(temporary.resolve()),landlock.HANDLED)]
    assert (str(declared.resolve()),landlock.READ) in rules
    assert all(access!=landlock.HANDLED for path,access in rules[2:])
    assert not any(path in {'/','/tmp'} for path,_ in rules)
    # /proc (already limited to own processes by the unit) is read-only, never write/execute.
    assert all(access==landlock.READ_FILE|landlock.READ_DIR for path,access in rules if path=='/proc')


def test_landlock_below_required_abi_fails_closed(monkeypatch):
    class Lib:
        def syscall(self,*args): return 2
    monkeypatch.setattr(landlock,'_libc',lambda:Lib())
    with pytest.raises(RuntimeError,match='abi_3_required'): landlock.abi()


@pytest.mark.skipif(sys.platform!='linux',reason='real Linux Landlock subprocess required')
def test_real_child_confines_symlinks_parents_hardlinks_proc_and_truncate(tmp_path):
    # On Linux a missing/broken ABI is a failure, not a successful skip.
    assert landlock.abi()>=3
    workspace=tmp_path/'workspace';workspace.mkdir()
    temporary=tmp_path/'private-tmp';temporary.mkdir()
    foreign=tmp_path/'other-lane';foreign.mkdir();(foreign/'private').write_text('untouched')
    (workspace/'symlink').symlink_to(foreign/'private')
    declared=tmp_path/'declared';declared.mkdir();(declared/'input').write_text('read only')
    script='''import os,sys
from pathlib import Path
from core.worker.landlock import install
w,t,out,ro=sys.argv[1:]
install(w,t,readonly=[ro])
assert Path(ro,'input').read_text()=='read only'
try: Path(ro,'input').write_text('bad')
except PermissionError: pass
else: raise AssertionError('declared input writable')
Path(w,'allowed').write_text('yes')
# Installed regression: tools and the identity probe read /proc/self; it stays read-only.
assert 'NoNewPrivs' in Path('/proc/self/status').read_text() and os.listdir('/proc/self/fd')
for p in ['/proc/self/attr/current','/proc/self/comm']:
 try: open(p,'w').write('x')
 except PermissionError: pass
 else: raise AssertionError(('proc write',p))
for p in [str(Path(w).parent/'other-lane/private'),str(Path(w)/'../other-lane/private'),str(Path(w)/'symlink'),'/proc/self/root'+out+'/private']:
 for mode in ['r','w']:
  try:
   with open(p,mode) as f: f.read() if mode=='r' else f.write('bad')
  except PermissionError: pass
  else: raise AssertionError(('escape',p,mode))
try: os.link(out+'/private',w+'/hardlink')
except (PermissionError,OSError): pass
else: raise AssertionError('hardlink escape')
'''
    result=subprocess.run([sys.executable,'-c',script,str(workspace),str(temporary),str(foreign),str(declared)],capture_output=True,text=True,
                          env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])})
    assert result.returncode==0,result.stderr
    assert (workspace/'allowed').read_text()=='yes' and (foreign/'private').read_text()=='untouched'


def test_run_workspace_output_is_group_readable_for_the_service_snapshot():
    # Installed regression: worker files were 0600, so the service's output
    # snapshot of a run workspace failed with not_found.
    from core.worker.daemon import job_umask
    assert job_umask('run_'+'a'*32)==0o007
    for other in ('scratch','projects','home'):
        assert job_umask(other)==0o077
