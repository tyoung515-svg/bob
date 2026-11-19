#!/usr/bin/env python3
"""Project-scoped stdio model adapter, not a live Bob/Hermes source edit.
Only two explicitly approved string-text inference aliases. No tools, image
transport, provider enrollment, global configuration, or automatic fallback.
"""
import json,sys,urllib.request,urllib.error
from pathlib import Path
ALLOWED={'bob-ds-flash':('DSv4.1 Flash','deepseek-v4-flash'),'bob-gpt-sol-61':('GPT SOL 6.1','gpt-6.1-sol')}
MAX_INPUT=80000

def handle(value,exchange):
 if not isinstance(value,dict):raise ValueError('invalid_request')
 if value=={'op':'catalog'}:
  data=exchange('GET','/v1/models',None)
  raw={m.get('id'):m.get('bob',{}) for m in data.get('data',[]) if isinstance(m,dict)}
  models=[]
  for id,(label,pin) in ALLOWED.items():
   b=raw.get(id,{})
   configured=b.get('enabled') is True and b.get('model')==pin and b.get('transport')=='text'
   models.append({'id':id,'label':label,'model_pin':pin,'configured':configured,'reachability':'unverified','image_input':False,'reason':None if configured else 'Not set up in the authenticated text-adapter catalog; no fallback'})
  return {'ok':True,'models':models}
 if set(value)!={'op','model','messages'} or value['op']!='complete' or value['model'] not in ALLOWED:raise ValueError('unapproved_model_or_request')
 messages=value['messages']
 if not isinstance(messages,list) or not 1<=len(messages)<=96:raise ValueError('invalid_messages')
 for m in messages:
  if not isinstance(m,dict) or set(m)!={'role','content'} or m['role'] not in ('system','user','assistant') or not isinstance(m['content'],str):raise ValueError('text_only_message_required')
 if len(json.dumps(value).encode())>MAX_INPUT:raise ValueError('input_too_large')
 model=value['model'];payload={'model':model,'messages':messages,'stream':False}
 data=exchange('POST','/v1/chat/completions',payload)
 # Never accept a silent alias/fallback mismatch as a selected-model completion.
 if data.get('model')!=model or data.get('bob',{}).get('model_pin')!=ALLOWED[model][1]:raise ValueError('resolved_model_mismatch_no_fallback')
 try:content=data['choices'][0]['message']['content']
 except (KeyError,IndexError,TypeError):raise ValueError('invalid_model_response')
 if not isinstance(content,str) or not content.strip() or len(content.encode())>200000:raise ValueError('invalid_model_content')
 return {'ok':True,'content':content,'requested_model':model,'model':data.get('model'),'completion_id':data.get('id'),'bob':data.get('bob',{}),'usage':data.get('usage',{}),'tool_execution':'client_owned_not_provided_by_relay','image_input':False}
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None

def main():
 try:
  raw=sys.stdin.buffer.read(MAX_INPUT+1)
  if len(raw)>MAX_INPUT:raise ValueError('input_too_large')
  value=json.loads(raw)
  def exchange(method,path,payload):
   token=(Path.home()/'.hermes/.bob-agent-token').read_text().strip()
   req=urllib.request.Request('http://127.0.0.1:7836'+path,method=method,data=None if payload is None else json.dumps(payload).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
   with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(req,timeout=95) as response:
    data=response.read(256001)
    if len(data)>256000:raise ValueError('response_too_large')
    return json.loads(data)
  result=handle(value,exchange);print(json.dumps(result,ensure_ascii=False),flush=True);return 0
 except urllib.error.HTTPError as e:result={'ok':False,'error':'bob_http_'+str(e.code)}
 except (OSError,TimeoutError):result={'ok':False,'error':'model_transport_unavailable_no_fallback'}
 except (ValueError,TypeError,KeyError):result={'ok':False,'error':'invalid_or_unapproved_model_response_no_fallback'}
 print(json.dumps(result),flush=True);return 1
if __name__=='__main__':sys.exit(main())
