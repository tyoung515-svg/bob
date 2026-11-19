import os,io,json,pathlib,runpy,sys,unittest
from unittest.mock import patch
D=pathlib.Path(__file__).resolve().parent
class Transport(unittest.TestCase):
 def test_fixed_argv_stdin_only_and_staggered_deadline(self):
  argv=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10','bob@example-server','python3','/opt/bob/relays/functional.py']
  data=b'{"messages":[{"role":"user","content":"synthetic; never a shell command"}]}'
  streams=[type('Stream',(),{'buffer':io.BytesIO()})() for _ in range(3)];streams[0].buffer=io.BytesIO(data)
  observed=[]
  def invoke(command,**kw):
   observed.append((command,kw));return type('R',(),{'returncode':0,'stdout':b'{"ok":true}','stderr':b''})()
  with patch.dict(os.environ,{'BOB_RELAY_TARGET':'bob@example-server','BOB_RELAY_ENTRY':'/opt/bob/relays/entry.py','BOB_FUNCTIONAL_RELAY':'/opt/bob/relays/functional.py','BOB_MODEL_RELAY':'/opt/bob/relays/models.py'}),patch.object(sys,'argv',argv),patch.object(sys,'stdin',streams[0]),patch.object(sys,'stdout',streams[1]),patch.object(sys,'stderr',streams[2]),patch('subprocess.run',side_effect=invoke):
   with self.assertRaises(SystemExit) as ended:runpy.run_path(str(D/'ssh'),run_name='__main__')
  self.assertEqual(ended.exception.code,0);self.assertEqual(len(observed),1)
  command,kw=observed[0];self.assertEqual(kw['timeout'],140);self.assertEqual(kw['input'],data)
  self.assertNotIn('synthetic',str(command));self.assertEqual(command[-1],'ds');self.assertIn('StrictHostKeyChecking=yes',command)
if __name__=='__main__':unittest.main()
