"""The broker carries native pulls/signatures, and has no approval or mode API."""
import pytest
from test_owner_session_broker import setup


@pytest.mark.parametrize('operation',['pc.approve','pc.mode.set','pc.grant','pc.run','pc.write'])
def test_forged_pc_grants_and_direct_execution_are_not_broker_operations(setup,operation):
    module,broker,state,*_=setup
    before=len(state['hits'])
    with pytest.raises(module.BrokerError,match='unknown_op'):
        broker.handle({'id':1,'op':operation,'args':{'approved':True,'mode':'yolo'}})
    assert len(state['hits'])==before


def test_pc_transport_does_not_accept_inline_grants(setup):
    module,broker,state,*_=setup
    with pytest.raises(module.BrokerError):
        broker.op_pc('pending',{'contract_version':2,'approved':True})
    before=len(state['hits'])
    for operation in ['pending','claim','receipt']:
        args={'contract_version':2} if operation=='pending' else {'contract_version':2,'payload':{'kind':'fixture'},'signature':'forged'}
        broker.op_pc(operation,args)
    assert [hit[2] for hit in state['hits'][before:]]==['/api/pc/pending','/api/pc/claim','/api/pc/receipt']
