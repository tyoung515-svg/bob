"""Typed model tools. Conversation, turn, project and mode are server selected."""
from langchain_core.tools import tool
from core.builder.service import invoke


@tool
async def fs_list(path: str = '') -> str:
    """List files inside the conversation project workspace."""
    return await invoke('fs_list',dict(path=path))


@tool
async def fs_read(path: str, offset: int = 0, max_bytes: int = 65536) -> str:
    """Read a bounded byte range from a project file."""
    return await invoke('fs_read',dict(path=path,offset=offset,max_bytes=max_bytes))


@tool
async def fs_search(pattern: str, glob: str = '*') -> str:
    """Search project files by regular expression and filename glob."""
    return await invoke('fs_search',dict(pattern=pattern,glob=glob))


@tool
async def fs_write(path: str, content: str) -> str:
    """Write any UTF-8 file type in the project, subject to server approval mode."""
    return await invoke('fs_write',dict(path=path,content=content))


@tool
async def fs_edit(path: str, old: str, new: str) -> str:
    """Replace one exact, unique string in a project file."""
    return await invoke('fs_edit',dict(path=path,old=old,new=new))


@tool
async def fs_delete(path: str) -> str:
    """Delete a project file, subject to server approval mode."""
    return await invoke('fs_delete',dict(path=path))


@tool
async def shell_run(command: str, cwd: str = '', timeout_s: int = 120, network: bool = False) -> str:
    """Local first: inspect memory, project files and BoB Library before fetching.

    Run only in the isolated worker (600 seconds), with bounded proxy networking.
    Unknown domains return durable owner approval cards; inspect network_log.
    """
    return await invoke('shell_run',dict(command=command,cwd=cwd,timeout_s=timeout_s,network=network))


@tool
async def preview_file(path: str) -> str:
    """Return file content up to 5 MB. Neither this tool nor the in-app preview
    executes JavaScript. The preview is a static picture, not an observation of
    clicks, animation or layout. For interaction use Open on this PC in the
    user's browser; if it does nothing in preview, explain this before editing.
    """
    return await invoke('preview_file',dict(path=path))


@tool
async def checkpoint_list() -> str:
    """List durable automatic snapshots taken before each turn's first mutation."""
    return await invoke('checkpoint_list',{})


@tool
async def checkpoint_restore(checkpoint_id: str) -> str:
    """Undo to an automatic project checkpoint, subject to server approval mode."""
    return await invoke('checkpoint_restore',dict(checkpoint_id=checkpoint_id))


@tool
async def workspace_create(name: str, purpose: str = '') -> str:
    """Request approval to create a lane project workspace and mount this conversation.

    Resets permissions to Approve. Use this tool instead of asking the user to
    create folders manually. Never accepts host paths.
    """
    return await invoke('workspace_create',dict(name=name,purpose=purpose))


@tool
async def workspace_mount(project_id: str) -> str:
    """Request approval to mount an existing registered project in this lane.

    The current conversation is server-selected. Old files are untouched and
    permissions reset to Approve. Only an exact owned project ID is accepted.
    A project record without a registered workspace cannot be mounted; call
    workspace_create with the app name instead, without retrying the mount.
    """
    return await invoke('workspace_mount',dict(project_id=project_id))


@tool
async def task_finish(report: str) -> str:
    """End the current Builder task and its approval scope after verification.

    Call only when the requested task is done, not at an intermediate turn end.
    Supply your final plain-language report (at most 6000 characters): what was
    built, where, what you checked and how, and what remains unverified.
    This report becomes your final reply; there is no further model round.
    This reduces authority; it never executes or retries a file operation.
    """
    return await invoke('task_finish',dict(report=report))


BUILDER_TOOLS = [task_finish,workspace_create,workspace_mount,fs_list,fs_read,fs_search,fs_write,fs_edit,fs_delete,shell_run,
                 preview_file,checkpoint_list,checkpoint_restore]
