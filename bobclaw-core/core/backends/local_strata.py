"""On-demand owner-PC Strata adapter. No inference lifecycle or cloud fallback."""

from core.worker import mode as execution_mode
from core.worker import client as worker_client
import asyncio,json,os
MODEL='qwen3.8-flash-next-iq3_xxs'
class LocalStrataOffline(RuntimeError):pass
class LocalStrataError(RuntimeError):pass
class LocalStrataConfigurationError(LocalStrataOffline):
 """A fixed configuration reason safe for the availability catalog."""
 pass
class LocalStrataClient:
 @staticmethod
 def parse(raw,rc,action='chat'):
  if rc:raise LocalStrataOffline('pc_connection_failed')
  try:d=json.loads(raw)
  except (ValueError,TypeError):raise LocalStrataError('invalid_local_response')
  if d.get('error'):raise LocalStrataOffline('local_backend_unavailable')
  if action=='status':
   if not d.get('available') or d.get('model')!=MODEL:raise LocalStrataOffline('model_not_loaded')
   return d
  if d.get('model')!=MODEL:raise LocalStrataError('local_model_mismatch')
  try:answer=d['choices'][0]['message']['content']
  except (KeyError,IndexError,TypeError):raise LocalStrataError('missing_local_answer')
  if not isinstance(answer,str) or not answer.strip():raise LocalStrataError('empty_local_answer')
  return answer
 async def request(self,payload):
  # Dual-host like the Claude route: the bridge is stdlib-only, so whichever PC
  # is awake can serve it (the Linux host first, then the Windows install).
     if execution_mode() == "worker":
         worker_client.refuse()
     from core.backends.claude_pc import choose_host, WINDOWS_HOST
     try: host=await choose_host()
     except Exception: raise LocalStrataConfigurationError('Local model is unavailable: configure the PC host.')
     setting = 'BOB_STRATA_WINDOWS_COMMAND' if host==WINDOWS_HOST else 'BOB_STRATA_LINUX_COMMAND'
     remote = os.environ.get(setting, '')
     if not remote.strip():
         raise LocalStrataConfigurationError('Local model is unavailable: set ' + setting + '.')
     if execution_mode() == "legacy":
         p=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,remote,
          stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
     else:
         p=await worker_client.refuse_async('ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,remote,
          stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
     try:out,_=await asyncio.wait_for(p.communicate(json.dumps(payload).encode()),timeout=180)
     except (TimeoutError,asyncio.CancelledError):p.kill();await p.wait();raise
     return self.parse(out.decode('utf-8-sig'),p.returncode,payload.get('action','chat'))
 async def chat(self,messages):return await self.request({'messages':messages})
 async def status(self):return await self.request({'action':'status'})
