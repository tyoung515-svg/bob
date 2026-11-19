"""ABI struct, exact syscall sequence, and fail-closed resource cleanup."""
import os
import pytest
from core.worker import landlock


@pytest.mark.parametrize('failure',[None,445,446])
def test_ruleset_construction_and_failure_closes_descriptors(tmp_path,monkeypatch,failure):
    path=tmp_path/'input';path.write_text('safe')
    readfd,writefd=os.pipe();os.close(writefd)
    calls=[]
    class Lib:
        def prctl(self,*args): calls.append(('prctl',args));return 0
        def syscall(self,number,*args):
            if number==444:
                assert args[0]._obj.handled_access_fs==landlock.HANDLED
                assert args[1]==8
                calls.append((number,landlock.HANDLED));return readfd
            if number==445:
                assert args[0]==readfd and args[1]==1
                rule=args[2]._obj
                assert rule.allowed_access==landlock.READ_FILE and rule.parent_fd>=0
                calls.append((number,rule.parent_fd))
            if number==446:
                assert args[0]==readfd and args[1]==0
                calls.append((number,readfd))
            return -1 if number==failure else 0
    monkeypatch.setattr(landlock,'abi',lambda:3)
    monkeypatch.setattr(landlock,'_libc',lambda:Lib())
    monkeypatch.setattr(landlock,'rules',lambda *a,**k:[(str(path),landlock.READ_FILE)])
    monkeypatch.setattr(os,'O_PATH',os.O_RDONLY,raising=False)
    monkeypatch.setattr(os,'O_CLOEXEC',getattr(os,'O_CLOEXEC',0),raising=False)
    if failure:
        with pytest.raises(RuntimeError): landlock.install(tmp_path,tmp_path)
    else: landlock.install(tmp_path,tmp_path)
    assert calls[0]==(444,landlock.HANDLED)
    assert any(call[0]==445 for call in calls)
    with pytest.raises(OSError): os.fstat(readfd)
    parent=next(call[1] for call in calls if call[0]==445)
    with pytest.raises(OSError): os.fstat(parent)


def test_unreachable_runtime_path_is_skipped_but_declared_input_fails_closed(tmp_path, monkeypatch):
    # Installed regression: in the worker unit, /etc/resolv.conf links into an
    # empty read-only /run and stat() raises PermissionError (not absence).
    from pathlib import Path
    from core.worker import landlock
    workspace, temporary, blocked = tmp_path / 'w', tmp_path / 't', tmp_path / 'blocked'
    workspace.mkdir(); temporary.mkdir(); blocked.mkdir()
    real = Path.resolve
    def resolve(self, strict=False):
        if str(self) == str(blocked):
            raise PermissionError(13, 'Permission denied', str(self))
        return real(self, strict=strict)
    monkeypatch.setattr(Path, 'resolve', resolve)
    paths = [p for p, _ in landlock.rules(workspace, temporary, runtime=(str(blocked),))]
    assert str(blocked) not in paths and str(workspace.resolve()) in paths
    import pytest
    with pytest.raises(RuntimeError, match='declared_input_unavailable'):
        landlock.rules(workspace, temporary, readonly=(str(blocked),), runtime=())
