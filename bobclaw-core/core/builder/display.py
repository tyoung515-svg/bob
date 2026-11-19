"""Human descriptions derived only from authoritative server records."""
from .policy import MODES, YOLO_NOTICE, command_segments, program


def approval_display(row, detail):
    workspace = detail.get('workspace_label') or 'this workspace'
    kind = row['kind']
    if kind == 'workspace':
        name = detail.get('name', detail.get('root_label', 'workspace'))
        title = (f'Create and switch to workspace “{name}” (empty folder in your lane)'
                 if detail.get('action') == 'create' else f'Switch to workspace “{name}”')
        lines = ['Permissions reset to Approve. Existing workspace files are kept.']
        scope = ('Allow once covers only this workspace change. Allow for task also covers writes, edits and bounded '
                 'checks in the new workspace for up to two hours. Deletes, restores and network still ask.')
    elif kind == 'network':
        title = f"Allow network access to {detail.get('domain', '')}"
        lines = [f"Method: {detail.get('method') or 'not visible (HTTPS CONNECT)'}",
                 'Network requests can send data to this host.']
        scope = 'Session allowance covers this host and method context only.'
    elif kind == 'command':
        title = f"Run: {detail.get('command', '')}"
        lines = [f"Working directory: {detail.get('cwd') or '.'} in {workspace}",
                 'Network: ' + ('requested; new hosts still need approval.' if detail.get('network') else 'no network permission added by this approval.')]
        segments = command_segments(detail.get('command'))
        programs = [program(w) for w, _ in segments] if segments else []
        if detail.get('exact_only_reason') or not programs or not all(programs):
            scope = detail.get('exact_only_reason') or 'Unsupported shell syntax; session approval covers only this exact command.'
        else:
            names = ', '.join(dict.fromkeys(' '.join(p) for p in programs))
            scope = f'Allow {names} in {workspace} for this session, with the same working directory and network context. Every command segment must be covered.'
    else:
        operation = detail.get('operation', kind)
        verb = 'Delete' if operation == 'fs_delete' else 'Write' if kind == 'write' else 'Edit'
        diff = detail.get('diff', '').splitlines()
        added = sum(line.startswith('+') and not line.startswith('+++') for line in diff)
        removed = sum(line.startswith('-') and not line.startswith('---') for line in diff)
        title = f"{verb} {detail.get('path', 'workspace files')}"
        if added or removed:
            title += f' (+{added}/−{removed} lines)'
        lines = [f'Workspace: {workspace}']
        if operation == 'checkpoint_restore':
            scope = 'Restore this checkpoint only; session approval covers this exact restore.'
        else:
            label = 'deletions' if operation == 'fs_delete' else 'edits'
            scope = ('This deletion requires its own approval.' if operation=='fs_delete' else
                     f'Allow writes, edits and bounded checks in {workspace} for this task. Protected paths are excluded; deletes, restores and network still ask.')
    if kind!='workspace':
        scope += ' Ends when the task finishes, you stop, workspace or mode changes, you log out, or after two hours.'
    return {'title': title, 'lines': lines, 'scope': scope}


def mode_display(current, requested=None):
    if requested is not None and requested not in MODES:
        from core.execution.models import ExecutionError
        raise ExecutionError('invalid_builder_mode')
    title = (f'Change Builder mode: {current.title()} → {requested.title()}'
             if requested else f'Change Builder mode (current: {current.title()})')
    lines = [YOLO_NOTICE] if requested == 'yolo' else []
    scope = ('Until this session ends or two hours, whichever comes first; then reverts to Auto.'
             if requested == 'yolo' else 'This conversation and its current workspace.')
    return {'title': title, 'lines': lines, 'scope': scope}
