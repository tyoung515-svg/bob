"""Owner inspection tools, with no writable counterpart or binding argument."""
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr


class Args(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ListArgs(Args):
    offset: StrictInt = 0
    limit: StrictInt = 20


class ProjectArgs(Args):
    project_id: StrictStr


class ProjectPath(ProjectArgs):
    path: StrictStr = ''


class ProjectRead(ProjectArgs):
    path: StrictStr
    offset: StrictInt = 0
    max_bytes: StrictInt = 65536


class ProjectSearch(ProjectArgs):
    pattern: StrictStr
    glob: StrictStr = '*'


class SourceArgs(Args):
    component: StrictStr


class SourcePath(SourceArgs):
    path: StrictStr = ''


class SourceRead(SourceArgs):
    path: StrictStr
    offset: StrictInt = 0
    max_bytes: StrictInt = 65536


class SourceSearch(SourceArgs):
    pattern: StrictStr
    glob: StrictStr = '*'


async def call(operation, args):
    import aiohttp
    import time
    from core.config import config
    from core.tools.workflow_context import current
    bound = current.get()
    if not bound or bound[0]['face_id'] not in {'assistant','planner'} or bound[0]['exp']<=time.time():
        return {'error':{'code':'inspection_context_unavailable'}}
    value, proof = bound
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100)) as session:
            async with session.post(f'http://127.0.0.1:{config.PORT}/api/chat-inspection/{operation}',
                    json={'context':value,'vouch':proof,'args':args}) as response:
                return await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return {'error':{'code':'inspection_unavailable'}}


@tool(args_schema=ListArgs)
async def projects_list(offset: int = 0, limit: int = 20) -> dict:
    """List this owner's projects, workspace registration, attached conversations and last activity. Follow next_offset."""
    return await call('projects_list', dict(offset=offset, limit=limit))


@tool(args_schema=ProjectArgs)
async def project_read(project_id: str) -> dict:
    """Read an owned project's description and brief or instructions; creates no workspace or binding."""
    return await call('project_read', dict(project_id=project_id))


@tool(args_schema=ProjectPath)
async def project_files_list(project_id: str, path: str = '') -> dict:
    """List an owned registered workspace using a relative path, without mounting it."""
    return await call('project_files_list', dict(project_id=project_id,path=path))


@tool(args_schema=ProjectRead)
async def project_files_read(project_id: str, path: str, offset: int = 0, max_bytes: int = 65536) -> dict:
    """Read a bounded range of a project file. Traversal, credentials and control paths are refused."""
    return await call('project_files_read', dict(project_id=project_id,path=path,offset=offset,max_bytes=max_bytes))


@tool(args_schema=ProjectSearch)
async def project_files_search(project_id: str, pattern: str, glob: str = '*') -> dict:
    """Search a registered project by regex in the isolated worker, with bounded output and no write operation."""
    return await call('project_files_search', dict(project_id=project_id,pattern=pattern,glob=glob))


@tool(args_schema=ProjectArgs)
async def project_git(project_id: str) -> dict:
    """Read repository presence, branch, branches, recent dated commit subjects and dirty state through offline git."""
    return await call('project_git', dict(project_id=project_id))


@tool(args_schema=Args)
async def bob_installation() -> dict:
    """Read this install's safe agents/models, permission rules, skills, workflows, PC evidence and build version."""
    return await call('bob_installation', {})


@tool(args_schema=SourcePath)
async def bob_source_list(component: str, path: str = '') -> dict:
    """List installed source in bobclaw-core, bobclaw-gateway, portal-broker, deploy-runtime or runtime-library. No Windows app source is installed."""
    return await call('bob_source_list', dict(component=component,path=path))


@tool(args_schema=SourceRead)
async def bob_source_read(component: str, path: str, offset: int = 0, max_bytes: int = 65536) -> dict:
    """Read bounded installed program text and its build provenance, excluding owner data and credentials."""
    return await call('bob_source_read', dict(component=component,path=path,offset=offset,max_bytes=max_bytes))


@tool(args_schema=SourceSearch)
async def bob_source_search(component: str, pattern: str, glob: str = '*') -> dict:
    """Search installed program text for a literal string, bounded to 2048 entries, four MiB and 100 matches."""
    return await call('bob_source_search', dict(component=component,pattern=pattern,glob=glob))


INSPECTION_TOOLS = [projects_list, project_read, project_files_list, project_files_read,
    project_files_search, project_git, bob_installation, bob_source_list, bob_source_read, bob_source_search]
