"""Actual broker read loop with a scripted clock; no server or model calls."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def module(monkeypatch,tmp_path):
    monkeypatch.setenv('BOB_PORTAL_STATE_DIR',str(tmp_path))
    spec=importlib.util.spec_from_file_location('c6_broker',Path(__file__).parents[1]/'portal-broker.py')
    value=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def drive(module,monkeypatch,frames):
    clock=[0.0]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.time,'time',lambda:1000+clock[0])
    events=[]
    sent=[]
    class Socket:
        def recv(self):
            seconds,value=next(frames)
            clock[0]+=seconds
            return 'text',json.dumps(value).encode(),None
        def send_json(self,value):sent.append(value)
    job=module.ChatJob(SimpleNamespace(emit=lambda name,data:events.append((name,data))), 'r','c','task',{},False)
    job.ws=Socket()
    job.budget=120
    job.deadline=120
    job._read_loop()
    return events,sent,clock[0]


def test_human_wait_over_150_seconds_does_not_spend_active_budget(module,monkeypatch):
    events,sent,elapsed=drive(module,monkeypatch,iter([
        (2,{'type':'builder_wait','waiting':True,'approval_id':'a','expires_at':1600}),
        (160,{'type':'builder_wait','waiting':False,'approval_id':'a'}),
        (20,{'type':'message_complete','message_id':'done'})]))
    assert elapsed==182 and events[-1][0]=='complete' and not sent
    assert events[0][1]['waiting'] and events[1][1]['waiting'] is False


def test_absolute_approval_expiry_still_ends_wait(module,monkeypatch):
    events,sent,_=drive(module,monkeypatch,iter([
        (2,{'type':'builder_wait','waiting':True,'expires_at':1005}),
        (4,{'type':'builder_wait','waiting':True,'expires_at':1600})]))
    assert events[-1]==('stopped',{'request_id':'r','code':'approval_expired'})
    assert not sent


def test_active_budget_after_approval_stops_server_generation(module,monkeypatch):
    events,sent,_=drive(module,monkeypatch,iter([
        (2,{'type':'builder_wait','waiting':True,'expires_at':1600}),
        (160,{'type':'builder_wait','waiting':False}),
        (119,{'type':'chunk','content':'late'})]))
    assert events[-1][1]['code']=='stream_deadline'
    assert sent==[{'type':'stop_generation','conversation_id':'c'}]


def test_disconnect_is_reported_without_replaying_mutations(module,monkeypatch):
    events=[]
    job=module.ChatJob(SimpleNamespace(emit=lambda n,d:events.append((n,d))),'r','c','task',{},False)
    job.ws=SimpleNamespace(recv=lambda:('close',b'',1006))
    job.deadline=module.time.monotonic()+120
    job._read_loop()
    assert events==[('stopped',{'request_id':'r','code':'connection_closed'})]
