"""Same all-operation fixtures for source, shipping snapshot, and upgraded app."""
from types import SimpleNamespace
from pathlib import Path
import pytest
from tests.test_execution_broker_v2 import broker

OPS=('attachments','list','start','audit','cancel','proposal','decide','artifact')
JID='a'*32


@pytest.fixture
def jobs_broker(broker):
    module,instance,hits=broker
    if not hasattr(instance,'_require_jobs'):
        pytest.skip('App copy predates tool-jobs/2; source/packaged remain required')
    if module.__file__ and Path(module.__file__).parent.name=='portal-broker':
        root=Path(module.__file__).resolve().parents[1]
        for relative in ('bobclaw-core/core/worker/landlock.py','bobclaw-core/core/tool_jobs/service.py',
                         'bobclaw-gateway/routers/tool_jobs.py','migrations/2026-11-04_tool_jobs.sql',
                         'bobclaw-core/core/execution/checked.py','bobclaw-core/core/execution/checked_work.py',
                         'bobclaw-core/core/tool_jobs/checked_runner.py','migrations/2026-11-04_z_checked_edits.sql'):
            assert (root/relative).is_file(),relative
    ordinary,owner=[],[]
    def rest(method,path,**kwargs):
        ordinary.append((method,path,kwargs))
        if path=='/capabilities':
            # Real /capabilities shape: tool_jobs carries the checked-edits coder negotiation.
            return {'operations':{'jobs.'+op:{'contract_versions':[2],'available':True} for op in OPS},
                    'tool_jobs':{'protocols':['tool-jobs/2'],'acceptance_revision':'checked-edits/1',
                                 'coder':{'available':True,'reason':None,'worker_contract':'checked-edits/1'}}}
        if '/artifacts/' in path:
            # Real service shape (core/tool_jobs/service.py): bounded base64 readback.
            import base64,hashlib
            name=path.rsplit('/',1)[1];data=b'{"bounded": true}';offset=kwargs.get('params',{}).get('offset',0)
            chunk=data[offset:offset+kwargs.get('params',{}).get('max_bytes',65536)];end=offset+len(chunk)
            return {'filename':name,'content':base64.b64encode(chunk).decode(),'encoding':'base64',
                    'sha256':hashlib.sha256(data).hexdigest(),'offset':offset,
                    'next_offset':end if end<len(data) else None,'total_bytes':len(data),'truncated':end<len(data)}
        return {'receipt':path,'content':'bounded','received':kwargs.get('body')}
    def owner_rest(method,path,**kwargs):
        owner.append((method,path,kwargs));return {'decision':{'state':'approved','applied':False}}
    instance.transport=SimpleNamespace(rest=rest)
    instance._owner_transport=SimpleNamespace(rest=owner_rest)
    instance._owner_session={'active':True,'expires_at':10**12}
    return module,instance,ordinary,owner


@pytest.mark.parametrize('op,args,method,path',[
 ('attachments',{},'GET','/api/tool-job-attachments'),('list',{},'GET','/api/tool-jobs'),
 ('start',{'contract_version':2,'role':'proposer','task':'Review','attachment':'registered','request_key':'keep-key',
           'team':'configured','team_revision':1,'budget':{'start_calls':10,'wall_mode':'fixed','wall_seconds':1800},
           'job_policy':{'proposed_seconds':900,'rationale':'report','auto_escalate':False}},'POST','/api/tool-jobs'),
 ('audit',{'job_id':JID},'GET','/api/tool-jobs/'+JID+'/audit'),
 ('cancel',{'job_id':JID,'request_key':'cancel-key'},'POST','/api/tool-jobs/'+JID+'/cancel'),
 ('proposal',{'job_id':JID},'GET','/api/tool-jobs/'+JID+'/proposal'),
 ('decide',{'contract_version':2,'job_id':JID,'decision':'approve','proposal_digest':'b'*64,'request_key':'decision-key','expected_revision':3},
  'POST','/api/tool-jobs/'+JID+'/proposal/decision'),
 ('artifact',{'job_id':JID,'filename':'proposal-'+JID+'.json','offset':1,'max_bytes':64},'GET','/api/tool-jobs/'+JID+'/artifacts/proposal-'+JID+'.json'),
])
def test_all_eight_operations_use_exact_route_and_transport(jobs_broker,op,args,method,path):
    module,instance,ordinary,owner=jobs_broker
    value=instance.handle({'id':1,'op':'jobs.'+op,'args':args}).value
    if op=='decide':
        assert owner[-1][:2]==(method,path)
        assert all(call[1]=='/capabilities' for call in ordinary)
        assert owner[-1][2]['body']['request_key']=='decision-key'
        assert value['decision']['applied'] is False
    else:
        assert ordinary[-1][:2]==(method,path) and owner==[]
        if op=='start': assert ordinary[-1][2]['body']==args


def test_decide_has_no_agent_transport_fallback(jobs_broker):
    module,instance,ordinary,owner=jobs_broker
    instance._owner_transport=None;instance._owner_session={}
    with pytest.raises(module.BrokerError,match='owner_confirmation_required'):
        instance.op_jobs_decide({'contract_version':2,'job_id':JID,'decision':'approve','proposal_digest':'b'*64,
                                 'request_key':'keep','expected_revision':1})
    assert all(call[1]=='/capabilities' for call in ordinary) and owner==[]


def test_legacy_start_is_refused_without_dispatch_or_reduction(jobs_broker):
    module,instance,ordinary,owner=jobs_broker
    with pytest.raises(module.BrokerError) as exc:
        instance.op_jobs_start({'role':'coder','task':'change','wall_seconds':120})
    assert exc.value.code=='client_upgrade_required' and ordinary==owner==[]


@pytest.mark.parametrize('op,args',[
 ('artifact',{'job_id':JID,'filename':'../secret'}),('audit',{'job_id':'../foreign'}),
 ('artifact',{'job_id':JID,'filename':'report-'+JID+'.json','max_bytes':65537}),
 ('proposal',{'job_id':JID,'owner':'foreign'})])
def test_no_arbitrary_resource_or_unbounded_read(jobs_broker,op,args):
    module,instance,ordinary,owner=jobs_broker
    with pytest.raises(module.BrokerError): instance.handle({'id':1,'op':'jobs.'+op,'args':args})
    assert ordinary==owner==[]


def test_checked_coder_forwards_negotiation_and_acceptance_without_reduction(jobs_broker):
    import inspect
    module,instance,ordinary,owner=jobs_broker
    if 'worker_contract' not in inspect.getsource(type(instance).op_jobs_start):
        pytest.skip('App copy predates checked-edits/1; source/packaged remain required')
    args={'contract_version':2,'role':'coder','task':'Checked edit','attachment':'registered','request_key':'keep-key',
          'team':'configured','team_revision':1,'worker_contract':'checked-edits/1',
          'acceptance':{'criteria':[{'id':'out','output_kind':'proposal','checks':{'required_check_ids':['python.pytest']}}]},
          'budget':{'start_calls':10,'wall_mode':'fixed','wall_seconds':3600,'max_job_attempts':8},
          'job_policy':{'proposed_seconds':1800,'rationale':'build','auto_escalate':True}}
    instance.op_jobs_start(args)
    assert ordinary[-1][2]['body']==args and owner==[]
    with pytest.raises(module.BrokerError,match='client_upgrade_required'):
        instance.op_jobs_start({k:v for k,v in args.items() if k!='worker_contract'})
