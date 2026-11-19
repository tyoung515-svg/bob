"""Test-only disposable HTTP transport; no live Bob calls."""
import importlib.util,json,tempfile,unittest
from pathlib import Path
SPEC=importlib.util.spec_from_file_location('relay',Path(__file__).resolve().parents[2]/'scripts/server/workbench-runtime-relay.py')
class RelayTests(unittest.TestCase):
 def load(self):
  self.assertTrue(SPEC and SPEC.loader and Path(SPEC.origin).exists(),'runtime relay missing')
  m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m);return m
 def test_dispatch_marker_prevents_replay_even_after_unknown_response(self):
  m=self.load()
  with tempfile.TemporaryDirectory() as d:
   calls=[]
   def failed(*args):calls.append(args);raise TimeoutError()
   request={'op':'start','request_id':'00000000-0000-0000-0000-000000000001','task':'synthetic'}
   for _ in range(2):
    with self.assertRaises(m.RelayError):m.handle(request,Path(d),failed)
   self.assertEqual(len(calls),1)
 def test_start_is_fixed_budget_and_duplicate_returns_owned_read(self):
  m=self.load()
  with tempfile.TemporaryDirectory() as d:
   calls=[];rid='00000000-0000-0000-0000-000000000002'
   def http(method,path,payload=None):
    calls.append((method,path,payload));return {'id':rid,'status':'running','receipts':[]}
   request={'op':'start','request_id':'00000000-0000-0000-0000-000000000001','task':'synthetic'}
   self.assertEqual(m.handle(request,Path(d),http)['id'],rid)
   self.assertEqual(m.handle(request,Path(d),http)['id'],rid)
   self.assertEqual([c[0] for c in calls],['POST','GET'])
   self.assertEqual(calls[0][2],{'team':'balance-budget','task':'synthetic','worker':'bob-ds-flash','max_model_calls':4,'max_seconds':180,'require_bulk':False})
   with self.assertRaises(m.RelayError):m.handle({**request,'task':'different'},Path(d),http)
 def test_invalid_identifiers_and_extra_fields_cannot_reach_transport(self):
  m=self.load()
  with tempfile.TemporaryDirectory() as d:
   def http(*args):self.fail('invalid request reached transport')
   for request in [{'op':'get','run_id':'../escape'},{'op':'start','task':'t','request_id':'bad'},{'op':'get','run_id':'00000000-0000-0000-0000-000000000002','token':'secret'}]:
    with self.assertRaises(m.RelayError):m.handle(request,Path(d),http)
if __name__=='__main__':unittest.main()
