"""UNIT transport-injected tests, never live/provider acceptance."""
import importlib.util, pathlib, unittest
path=pathlib.Path(__file__).resolve().parents[2]/'scripts/server/workbench-model-relay.py'
spec=importlib.util.spec_from_file_location('model_relay',path);relay=importlib.util.module_from_spec(spec);spec.loader.exec_module(relay)
class RelayTests(unittest.TestCase):
 def test_selected_route_forwarded_and_actual_pin_required(self):
  observed=[]
  def exchange(method,path,payload):
   observed.append(payload)
   return {'model':'bob-gpt-sol-61','id':'unit-only','bob':{'model_pin':'gpt-6.1-sol'},'choices':[{'message':{'content':'unit response'}}]}
  result=relay.handle({'op':'complete','model':'bob-gpt-sol-61','messages':[{'role':'user','content':'context preserved'}]},exchange)
  self.assertEqual(observed[0]['model'],'bob-gpt-sol-61');self.assertEqual(observed[0]['messages'][0]['content'],'context preserved');self.assertEqual(result['requested_model'],'bob-gpt-sol-61');self.assertEqual(result['bob']['model_pin'],'gpt-6.1-sol')
  with self.assertRaises(ValueError):relay.handle({'op':'complete','model':'bob-gpt-sol-61','messages':[{'role':'user','content':'x'}]},lambda *a:{'model':'bob-ds-flash','bob':{'model_pin':'deepseek-v4-flash'},'choices':[{'message':{'content':'wrong'}}]})
 def test_unapproved_or_image_never_reaches_transport(self):
  for model in ['deepseek-v4-pro','bob-qwen-strata','bob-kimi-k3','unknown']:
   with self.assertRaises(ValueError):relay.handle({'op':'complete','model':model,'messages':[{'role':'user','content':'x'}]},lambda *a:self.fail('must not dispatch'))
  with self.assertRaises(ValueError):relay.handle({'op':'complete','model':'bob-gpt-sol-61','messages':[{'role':'user','content':[{'type':'image_url','image_url':'x'}]}]},lambda *a:self.fail('must not dispatch'))
 def test_catalog_is_filtered_configured_not_reachability(self):
  result=relay.handle({'op':'catalog'},lambda *a:{'data':[{'id':'bob-gpt-sol-61','bob':{'enabled':True,'model':'gpt-6.1-sol','transport':'text'}},{'id':'bob-qwen-strata','bob':{'enabled':True}}]})
  choices={m['id']:m for m in result['models']};self.assertEqual(set(choices),{'bob-ds-flash','bob-gpt-sol-61'});self.assertTrue(choices['bob-gpt-sol-61']['configured']);self.assertFalse(choices['bob-gpt-sol-61']['image_input']);self.assertFalse(choices['bob-ds-flash']['configured']);self.assertEqual(choices['bob-gpt-sol-61']['reachability'],'unverified')
if __name__=='__main__':unittest.main()
