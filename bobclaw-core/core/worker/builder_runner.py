"""builder-command/1. Launched ONLY by the proxy-bound Landlocked profile."""
import base64
import json
import os
from pathlib import Path
import sys

from core.portal_resources import relative_parts, safe_open, filesystem_stage
from core.execution.models import ExecutionError
from core.builder.files import read, search

ADAPTER = 'builder-command/1'


def put(root, path, data):
    parts = relative_parts(path)
    # Create ancestors through checked dirfds, never recursive path-following.
    for n in range(1, len(parts)):
        with safe_open(root, parts[:n-1], directory=True) as (fd, parent, _):
            with filesystem_stage('mkdir'):
                try:
                    if fd is None:
                        (parent / parts[n-1]).mkdir()
                    else:
                        # RestrictSUIDSGID rejects mkdir's explicit S_ISGID bit
                        # before even reporting EEXIST. The kernel inherits
                        # setgid/default ACLs from the managed parent instead.
                        os.mkdir(parts[n-1], mode=0o770, dir_fd=fd)
                except FileExistsError:
                    pass
        with safe_open(root, parts[:n], directory=True):
            pass
    from core.worker.workspace import write
    # workspace.write checks the parent with no-follow traversal, and replaces
    # the final directory entry atomically rather than following a final link.
    target = root.joinpath(*parts)
    if target.exists() or target.is_symlink():
        with safe_open(root, parts) as (_, _, info):
            if info.st_nlink != 1:
                exc = ExecutionError('shared_file_refused', 403)
                exc.diagnostics = {'stage':'nlink-check','errno':None}
                raise exc
    if os.name == 'nt':
        target.write_bytes(data)  # test host only; never executes commands here
    else:
        import secrets
        with safe_open(root, parts[:-1], directory=True) as (fd, _, _):
            name = 'builder-' + secrets.token_hex(16)
            with filesystem_stage('temp-open'):
                tmp = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660, dir_fd=fd)
            try:
                with filesystem_stage('temp-write-fsync'):
                    with os.fdopen(tmp, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                with filesystem_stage('replace'):
                    os.replace(name, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass


def remove(root, path, *, directory=False):
    parts = relative_parts(path)
    with safe_open(root, parts, directory=directory):
        pass
    with safe_open(root, parts[:-1], directory=True) as (fd, parent, _):
        if fd is None:
            (parent / parts[-1]).rmdir() if directory else (parent / parts[-1]).unlink()
        elif directory:
            os.rmdir(parts[-1], dir_fd=fd)
        else:
            os.unlink(parts[-1], dir_fd=fd)


def execute(payload, root):
    root = Path(root).absolute()
    operation, args = payload['operation'], payload['args']
    if operation in {'fs_write','fs_edit','fs_delete'} and '_preimage' in args:
        import hashlib
        from core.portal_resources import ResourceError
        try:
            current, size = read(root,args['path'])
            actual=hashlib.sha256(current).hexdigest()
            if size>len(current):raise ExecutionError('builder_preimage_changed',409)
        except ResourceError as exc:
            if exc.status!=404:raise
            actual=None
        if actual != args['_preimage']:
            raise ExecutionError('builder_preimage_changed',409)
    if operation == 'shell_run':
        if os.name != 'posix':
            raise ExecutionError('isolated_linux_worker_required', 503)
        env = dict(os.environ)
        if payload.get('network'):
            from urllib.parse import urlsplit
            proxy = payload.get('proxy_url','')
            url = urlsplit(proxy)
            if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username != 'bob'
                    or not url.password or len(url.password)!=64 or not url.port or url.path):
                raise ExecutionError('invalid_builder_proxy',503)
            for key in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy'):
                env[key] = proxy
            env['NO_PROXY'] = env['no_proxy'] = ''
            env['npm_config_audit'] = 'false'
        else:
            for key in list(env):
                if key.lower().endswith('_proxy'):
                    env.pop(key)
        parts = relative_parts(args.get('cwd', ''), empty=True)
        with safe_open(root, parts, directory=True) as (fd, path, _):
            os.fchdir(fd) if fd is not None else os.chdir(path)
        os.execve('/bin/sh', ['/bin/sh', '-c', args['command']], env)
    if operation == 'fs_search':
        return search(root, args['pattern'], args.get('glob', '*'),
            read_scope=args.get('_read_scope'), inspection=bool(args.get('_inspection')))
    if operation == 'fs_write':
        data = args['content'].encode('utf-8')
        if len(data) > 256 * 1024:
            raise ExecutionError('write_too_large', 413)
        put(root, args['path'], data)
        return {'path': args['path'], 'bytes': len(data)}
    if operation == 'fs_edit':
        data, size = read(root, args['path'])
        if size > len(data):
            raise ExecutionError('edit_too_large', 413)
        text = data.decode('utf-8')
        old = args['old']
        if not old or text.count(old) != 1:
            raise ExecutionError('edit_match_not_unique', 409)
        return execute({'operation': 'fs_write', 'args': {'path': args['path'],
                       'content': text.replace(old, args['new'], 1)}}, root)
    if operation == 'fs_delete':
        remove(root, args['path'])
        return {'path': args['path'], 'deleted': True}
    if operation == 'checkpoint_restore':
        from core.builder.files import checkpoint_snapshot
        frozen = args['snapshot']
        # Validate the entire immutable payload before removing anything.
        verified = {}
        for item in frozen['manifest']:
            relative_parts(item['path'])
            if item['kind'] != 'file':
                continue
            data = base64.b64decode(frozen['blobs'][item['sha256']], validate=True)
            if frozen.get('encoding') == 'zlib-base64':
                import zlib
                decoder=zlib.decompressobj()
                data=decoder.decompress(data,4*1024*1024+1)
                if len(data)>4*1024*1024 or decoder.unconsumed_tail or not decoder.eof:
                    raise ExecutionError('checkpoint_blob_too_large',413)
            import hashlib
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ExecutionError('checkpoint_digest_conflict',409)
            verified[item['sha256']]=data
        current = checkpoint_snapshot(root)
        keep = {r['path']: r for r in frozen['manifest']}
        for item in reversed(current['manifest']):
            target = keep.get(item['path'])
            if not target or target['kind'] != item['kind']:
                remove(root, item['path'], directory=item['kind'] == 'directory')
        for item in frozen['manifest']:
            if item['kind'] == 'file':
                put(root, item['path'], verified[item['sha256']])
                if os.name == 'posix':
                    with safe_open(root, relative_parts(item['path'])) as (fd, _, _):
                        os.fchmod(fd, 0o770 if item['mode'] & 0o111 else 0o660)
            else:
                # put creates parents; preserve empty directories as well.
                parts = relative_parts(item['path'])
                with safe_open(root, parts[:-1], directory=True) as (fd, parent, _):
                    try:
                        os.mkdir(parts[-1], mode=0o770, dir_fd=fd) if fd is not None else (parent / parts[-1]).mkdir()
                    except FileExistsError:
                        pass
        return {'restored': True, 'digest': frozen['digest']}
    raise ExecutionError('unsupported_builder_operation')


def main():
    payload = json.load(sys.stdin)
    try:
        result = execute(payload, Path.cwd())
        identity = {'stage':'finished','errno':None}
        if hasattr(os,'getuid'):
            identity.update(uid=os.getuid(),gid=os.getgid(),groups=os.getgroups())
        print(json.dumps({'adapter': ADAPTER, 'result': result,'diagnostics':identity}))
    except Exception as exc:
        diagnostic = dict(getattr(exc, 'diagnostics', None) or {'stage': 'execute', 'errno': None})
        if hasattr(os, 'getuid'):
            diagnostic.update(uid=os.getuid(), gid=os.getgid(), groups=os.getgroups())
        print(json.dumps({'adapter': ADAPTER, 'error': getattr(exc, 'code', 'io_error'),
                          'diagnostics': diagnostic}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
