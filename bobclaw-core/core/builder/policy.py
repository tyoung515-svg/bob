"""Server-owned Builder decisions and bounded, versioned session scopes."""
import re
import shlex
import time

MODES = frozenset({'suggest', 'approve', 'auto', 'yolo'})
KINDS = frozenset({'write', 'edit', 'command', 'network', 'workspace'})
YOLO_NOTICE = ('May delete or overwrite workspace files, run arbitrary code, download '
               'packages, use network and spend model calls; mistakes are possible.')


def workspace_path(path, *, empty=False):
    from core.portal_resources import relative_parts, ResourceError
    try:
        relative_parts(path, empty=empty)
        return True
    except ResourceError:
        return False


def command_segments(command):
    """Deliberately incomplete shell grammar. None means exact approval only.

    Quoted punctuation may conservatively fail parsing, but never confers scope.
    No shell interpretation or subprocess is used for policy decisions.
    """
    if (not isinstance(command, str) or not command.strip() or
            any(c in command for c in '`$\\\x00\r(){}#') or '<<' in command):
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>')
    lexer.whitespace = ' \t'
    lexer.commenters = ''
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments, words, writes = [], [], False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {';', '&&', '||', '|', '\n'}:
            if not words:
                if token == '\n':
                    i += 1
                    continue
                return None
            segments.append((words, writes))
            words, writes = [], False
        elif token in {'>', '>>', '<', '>&', '<&'}:
            if words and words[-1].isdigit():
                words.pop()
            i += 1
            if i >= len(tokens):
                return None
            target = tokens[i]
            if token in {'>&', '<&'}:
                if target not in {'0', '1', '2'}:
                    return None
            elif target != '/dev/null' and (any(c in target for c in ';&|<>\n') or not workspace_path(target)):
                return None
            writes |= token in {'>', '>>'}
        elif any(c in token for c in '\n;&|<>'):
            return None
        else:
            operand = token.split('=', 1)[-1] if '=' in token else token
            if operand != '.' and (operand.startswith(('/', '~')) or
                    '..' in operand.split('/') or
                    ('/' in operand or operand.startswith('.')) and
                    not workspace_path(operand.removeprefix('./'))):
                return None
            words.append(token)
        i += 1
    if words:
        segments.append((words, writes))
    elif tokens and next((t for t in reversed(tokens) if t != '\n'), ';') in {'&&', '||', '|'}:
        return None
    if not segments:
        return None
    forbidden = {'eval', 'exec', 'source', 'sh', 'bash', 'dash', 'zsh', 'env', 'command', 'xargs',
                 'if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'until', 'do', 'done',
                 'case', 'esac', 'in', 'function', 'time', 'coproc', 'select', 'cd', 'export',
                 'unset', 'set', 'trap', 'alias', 'unalias', 'readonly', 'return', 'break',
                 'continue', 'wait', 'bg', 'fg', 'kill', 'ulimit', 'umask', 'read'}
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', w[0]) or w[0] in forbidden for w, _ in segments):
        return None
    return segments


def program(words):
    if words[0] in {'git', 'npm', 'pnpm', 'yarn'}:
        if len(words) < 2 or words[1].startswith('-'):
            return None
        if words[1] == 'run':
            return words[:3] if len(words) > 2 and not words[2].startswith('-') else None
        return words[:2]
    if words[0] in {'python', 'python3'} and words[1:2] == ['-m']:
        return words[:3] if len(words) > 2 and not words[2].startswith('-') else None
    return words[:1]


def read_only_command(command):
    segments = command_segments(command)
    if not segments:
        return False
    options = {
        'pwd': {'-L', '-P'}, 'ls': {'--all', '--long', '--human-readable'},
        'cat': {'-n', '-b', '-s', '-A'}, 'head': {'-n', '-c'}, 'tail': {'-n', '-c'},
        'wc': {'-l', '-w', '-c', '-m', '-L'}, 'echo': {'-n', '-e', '-E'},
        'grep': {'-n', '-r', '-R', '-i', '-l', '-v', '-E', '-F', '-e', '-q', '-c', '-w'},
        'rg': {'-n', '-i', '-l', '-v', '-e', '-q', '-c', '-w', '--files', '--glob', '-g', '--fixed-strings'},
        'find': {'-name', '-iname', '-path', '-type', '-maxdepth', '-mindepth', '-print', '-print0', '-size', '-mtime', '-o', '-a', '-not'},
        'stat': {'-c', '--format', '-f'}, 'file': {'-b', '-i', '--mime', '--mime-type'},
        'diff': {'-u', '-r', '-q', '-s', '-w', '-b', '--stat'},
    }
    for words, writes in segments:
        if writes:
            return False
        name, args = words[0], words[1:]
        if name == 'git':
            if args[:1] not in (['status'], ['log'], ['diff'], ['show']):
                return False
            args = args[1:]
            allowed = {'--short', '--stat', '--name-only', '--cached', '--no-ext-diff', '--no-textconv', '--oneline', '--all', '--patch', '-p'}
        elif name in options:
            allowed = options[name]
        else:
            return False
        for arg in args:
            if name == 'echo' or not arg.startswith('-') or arg == '--' or arg in allowed:
                continue
            if name == 'ls' and re.fullmatch(r'-[alhnrtSdF]+', arg):
                continue
            if name in {'head', 'tail', 'git'} and re.fullmatch(r'-[0-9]+', arg):
                continue
            return False
    return True


def safe_command(command):
    # Preserve the existing auto test-runner safe set. All execution still
    # requires the isolated worker, including commands classified read-only.
    if read_only_command(command):
        return True
    segments = command_segments(command)
    if not segments or len(segments) != 1 or segments[0][1]:
        return False
    words = segments[0][0]
    if words[:3] in (['python', '-m', 'pytest'], ['python3', '-m', 'pytest']):
        return all(not w.startswith('-') or w in {'-q', '-v', '-vv', '-x', '--disable-warnings'} for w in words[3:])
    return words == ['npm', 'test']


def allowance_keys(kind, detail):
    from core.execution.models import digest
    context = [detail.get('workspace', ''), detail.get('cwd', ''), bool(detail.get('network'))]
    if kind in {'write', 'edit'} and workspace_path(detail.get('path')):
        if detail.get('operation') in {'fs_write','fs_edit'}:
            return [task_allowance(detail.get('workspace',''))]
        # Delete/restore never inherit a task write grant.
        return [digest(['builder-scope/2', kind, detail.get('operation', kind), context[0]])]
    if kind == 'command' and not detail.get('exact_only_reason'):
        segments = command_segments(detail.get('command'))
        if segments and workspace_path(detail.get('cwd', ''), empty=True):
            programs = [program(words) for words, _ in segments]
            if all(programs):
                return [digest(['builder-scope/2', kind, p, context]) for p in programs]
    return [digest(['builder-exact/2', kind, detail])]


def allowance_key(kind, detail):
    """Compatibility helper for callers approving one simple command."""
    return allowance_keys(kind, detail)[0]


def session_allowed(kind, detail, allowances):
    if detail.get('operation') in {'fs_delete','checkpoint_restore'}:
        return False
    if kind in {'write', 'edit'} and detail.get('operation') != 'checkpoint_restore' and not workspace_path(detail.get('path')):
        return False
    if (kind == 'command' and not detail.get('network') and not detail.get('exact_only_reason')
            and task_check(detail.get('command')) and workspace_path(detail.get('cwd',''),empty=True)
            and task_allowance(detail.get('workspace','')) in allowances):
        return True
    return all(key in allowances for key in allowance_keys(kind, detail))


def task_allowance(workspace):
    from core.execution.models import digest
    return digest(['builder-task/3',workspace,'writes-edits-checks'])


def task_check(command):
    if safe_command(command):
        return True
    segments = command_segments(command)
    if not segments or len(segments)!=1 or segments[0][1]:
        return False
    words = segments[0][0]
    if words[:3] in (['python','-m','unittest'],['python3','-m','unittest']):
        return all(not w.startswith('-') or w in {'-v','-q','-s','-p','-t','-f','-b','--failfast'} for w in words[3:])
    if words[:3] in (['python','-m','compileall'],['python3','-m','compileall']):
        return all(not w.startswith('-') or w in {'-q','-f'} for w in words[3:])
    return len(words)==3 and words[:2]==['node','--check'] and workspace_path(words[2])


def decide(mode, kind, detail, session_allowances=()):
    now = time.time()
    if isinstance(mode, dict):
        mode = 'auto' if mode['mode'] == 'yolo' and (mode.get('expires_at') or 0) <= now else mode['mode']
    if mode not in MODES or kind not in KINDS:
        return 'refuse'
    if mode == 'suggest':
        return 'propose'
    if kind == 'workspace':
        return 'run' if mode == 'yolo' else 'ask'
    if mode == 'yolo' or session_allowed(kind, detail, session_allowances):
        return 'run'
    if kind == 'command' and not detail.get('network') and not detail.get('exact_only_reason') and read_only_command(detail.get('command')):
        return 'run'
    if mode == 'auto' and (kind in {'write', 'edit'} or
            kind == 'command' and not detail.get('network') and not detail.get('exact_only_reason') and safe_command(detail.get('command'))):
        return 'run'
    return 'ask'


def command_scope_reason(root, detail):
    """Do not inherit broad command approval through a symlink operand or cwd.

    This is conservative admission, not the execution boundary. The existing
    worker still rechecks cwd and enforces Landlock on every shell operation.
    """
    import stat
    from pathlib import Path
    segments = command_segments(detail.get('command'))
    if not segments or not all(program(words) for words, _ in segments):
        return 'Shell syntax or paths cannot be safely scoped; only this exact command is covered.'
    lexer = shlex.shlex(detail['command'], posix=True, punctuation_chars=';&|<>')
    lexer.whitespace = ' \t'
    lexer.commenters = ''
    cwd = detail.get('cwd', '')
    if not workspace_path(cwd, empty=True):
        return 'Working directory is outside the workspace scope.'
    operands = [cwd] + [str(Path(cwd) / token.split('=', 1)[-1]) for token in lexer
                        if token and token[0] not in ';&|<>\n' and token != '/dev/null']
    for operand in operands:
        target = Path(root)
        for part in Path(operand).parts:
            target /= part
            try:
                info = target.lstat()
            except FileNotFoundError:
                break
            except OSError:
                return 'A command path cannot be verified; only this exact command is covered.'
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return 'A command path is a symlink; only this exact command is covered.'
    return None
