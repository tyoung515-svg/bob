import importlib.util,io,json,pathlib,shutil,sys,tempfile,unittest
from unittest.mock import patch
D=pathlib.Path(__file__).resolve().parent
class BudgetBoundary(unittest.TestCase):
 def setUp(self):
  self.root=pathlib.Path(tempfile.mkdtemp(prefix='ui-budget-test-'))
  shutil.copyfile(D/'call_ledger.py',self.root/'call_ledger.py')
  with (self.root/'call_ledger.py').open('a') as instrument:
   instrument.write('\n_original_resolve=resolve\ndef resolve(root,lane,ident,state,receipt):\n p=root/"resolve-count"\n p.write_text(str((int(p.read_text()) if p.exists() else 0)+1))\n return _original_resolve(root,lane,ident,state,receipt)\n')
  self.return_bytes=b'{"ok":true,"content":"synthetic"}'
  spec=importlib.util.spec_from_file_location('adapter',D/'ui_budget_relay.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m);self.m.D=self.root;self.m.ROUTES={'ds':'/opt/bob/relays/functional.py','models':'/opt/bob/relays/models.py'}
 def invoke(self):
  stdin=type('Input',(),{'buffer':io.BytesIO(b'{"messages":[{"role":"user","content":"synthetic"}]}')})()
  stdout=type('Output',(),{'buffer':io.BytesIO()})()
  def run(*a,**kw):
   rows=json.loads((self.root/'UI-CALLS.json').read_text());assert rows[-1]['state']=='reserved'
   return type('Result',(),{'returncode':0,'stdout':self.return_bytes,'stderr':b''})()
  with patch.object(sys,'argv',['adapter','ds']),patch.object(sys,'stdin',stdin),patch.object(sys,'stdout',stdout),patch.object(self.m.subprocess,'run',side_effect=run) as dispatched:
   result=self.m.main();self.assertEqual(dispatched.call_count,1);return result
 def test_missing_phase_allocation_refuses_before_dispatch(self):
  with self.assertRaisesRegex(ValueError,'allocation'):self.invoke()
  self.assertFalse((self.root/'UI-CALLS.json').exists())
 def test_oversized_response_resolves_exactly_once_unknown(self):
  (self.root/'UI-ALLOCATION.json').write_text('{"effective_ui_cap":8}')
  self.return_bytes=b'x'*256001
  with self.assertRaisesRegex(ValueError,'too large'):self.invoke()
  self.assertEqual((self.root/'resolve-count').read_text(),'1')
  self.assertEqual(json.loads((self.root/'UI-CALLS.json').read_text())[0]['state'],'unknown')
 def test_catalog_exemption_rebuilds_exact_read_only_body(self):
  stdin=type('I',(),{'buffer':io.BytesIO(b'{"op":"catalog","messages":[{"role":"user","content":"must not forward"}]}')})()
  stdout=type('O',(),{'buffer':io.BytesIO()})()
  observed=[]
  def catalog(command,**kw):
   observed.append(json.loads(kw['input']));return type('R',(),{'returncode':0,'stdout':b'{"ok":true,"models":[]}', 'stderr':b''})()
  with patch.object(sys,'argv',['adapter','models']),patch.object(sys,'stdin',stdin),patch.object(sys,'stdout',stdout),patch.object(self.m.subprocess,'run',side_effect=catalog):self.assertEqual(self.m.main(),0)
  self.assertEqual(observed,[{'op':'catalog'}]);self.assertFalse((self.root/'UI-CALLS.json').exists())
 def test_reservation_exists_inside_dispatch(self):
  (self.root/'UI-ALLOCATION.json').write_text('{"effective_ui_cap":8}')
  self.assertEqual(self.invoke(),0)
  rows=json.loads((self.root/'UI-CALLS.json').read_text());self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'achieved')
 def test_phase_cap_exhaustion_no_dispatch(self):
  (self.root/'UI-ALLOCATION.json').write_text('{"effective_ui_cap":8}')
  (self.root/'UI-CALLS.json').write_text(json.dumps([{'id':str(i)} for i in range(8)]))
  with self.assertRaisesRegex(ValueError,'exhausted'):self.invoke()
  self.assertEqual(len(json.loads((self.root/'UI-CALLS.json').read_text())),8)
if __name__=='__main__':unittest.main()
