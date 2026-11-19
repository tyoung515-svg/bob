"""Every runtime agent can propose PC work; only the native app may grant it."""
import json
import ntpath
import time
from langchain_core.tools import tool
from core.execution.models import Authority, ExecutionError

PC_TOOL_NAMES = ['pc_run','pc_read','pc_write','pc_list','pc_open','pc_info','pc_task_finish']


async def invoke(operation,args,key):
    from core.execution.pc_context import current
    from core.execution.chat_records import POOL
    from core.execution.owner import check_epoch
    from core.execution.pc import PC
    from core.tools.ask import _writer
    context=current.get()
    if not context:
        return json.dumps({'status':'failed','error':{'code':'pc_context_unavailable'}})
    if context['exp']<=time.time():
        return json.dumps({'status':'failed','error':{'code':'pc_turn_window_ended',
            'detail':'This turn has been open longer than its 10-minute PC window. Nothing is wedged. PC work can continue after the user sends a new message.'}})
    if POOL is None:
        return json.dumps({'status':'failed','error':{'code':'pc_context_unavailable'}})
    try:
        await check_epoch(Authority(**context['authority']))
        if operation=='pc.task.finish':
            auth=Authority(**context['authority'])
            task=await PC(POOL).task(auth,context['face_id'],context['conversation_id'])
            await POOL.execute('UPDATE pc_tasks SET ended_at=$2 WHERE id=$1',task['id'],time.time())
            await POOL.execute("UPDATE pc_requests SET state=CASE WHEN state='running' THEN 'unknown' ELSE 'stopped' END WHERE task_id=$1 AND state IN('pending','running')",task['id'])
            return json.dumps({'status':'done','task_id':task['id'],'detail':'PC task ended; request a fresh native grant for later work.'})
        return json.dumps(await PC(POOL).request(context,operation,args,key,writer=_writer.get()))
    except ExecutionError as e:
        return json.dumps({'status':'failed','error':{'code':e.code}})


@tool
async def pc_run(shell:str,argv:list[str],cwd:str,timeout_s:int,request_key:str)->str:
    """Request one Windows script with an explicit powershell or cmd shell and timeout.

    argv is passed TO the chosen shell, never the shell name: PowerShell uses
    ["-Command", "<script>"], cmd uses ["/C", "<command>"]. The app adds
    -NoLogo -NoProfile -NonInteractive itself for PowerShell. One script per call.
    Native approval is required; never claim it ran before the native receipt.
    """
    names = {'powershell': {'powershell', 'powershell.exe', 'pwsh', 'pwsh.exe'},
             'cmd': {'cmd', 'cmd.exe'}}
    if argv and ntpath.basename(argv[0]).lower() in names.get(shell, set()):
        argv = argv[1:]
    if not argv:
        return json.dumps({'status':'failed','error':{'code':'invalid_pc_command',
            'detail':'argv must contain arguments for the chosen shell, not just its name.'}})
    return await invoke('pc.run',dict(shell=shell,argv=argv,cwd=cwd,timeout_s=timeout_s),request_key)

@tool
async def pc_read(path:str,request_key:str)->str:
    """Request a Windows file read in the native app under this agent's task grant."""
    return await invoke('pc.read',dict(path=path),request_key)

@tool
async def pc_write(path:str,content:str,request_key:str,preimage_sha256:str|None=None)->str:
    """Request an atomic Windows write. Supply the read receipt's SHA256 to edit; null means create-only. The native Windows app decides and executes."""
    return await invoke('pc.write',dict(path=path,content=content,preimage_sha256=preimage_sha256),request_key)

@tool
async def pc_list(path:str,request_key:str)->str:
    """Request a Windows directory listing; auto mode permits reads/lists within the native task scope."""
    return await invoke('pc.list',dict(path=path),request_key)

@tool
async def pc_open(path:str,request_key:str)->str:
    """Ask the native Windows app to open a file or HTTP(S) URL in its default app."""
    return await invoke('pc.open',dict(path=path),request_key)

@tool
async def pc_info(request_key:str)->str:
    """Request a fixed, read-only summary of this PC collected by the Windows app itself.

    Includes processor, graphics and its memory, system memory, disk space and
    Windows version. Use instead of pc_run for hardware questions. Native
    approval rules still apply; wait for the native receipt before reporting it.
    """
    return await invoke('pc.info',{},request_key)

@tool
async def pc_task_finish()->str:
    """End this agent's PC task and its native task grants, retaining receipts."""
    return await invoke('pc.task.finish',{},'finish')

PC_TOOLS=[pc_run,pc_read,pc_write,pc_list,pc_open,pc_info,pc_task_finish]
