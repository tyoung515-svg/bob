"""Backend-only turn clocks; fake time, no model or native PC execution."""
import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_day_one_broker_sync import BACKEND, load_broker


@pytest.fixture(autouse=True)
def _isolated_gateway_db():
    yield  # stdlib broker tests do not use the application DB


def make_job(monkeypatch, frames):
    module = load_broker(BACKEND / 'portal-broker/portal-broker.py', monkeypatch)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: 10000+clock.now))
    emitted, sent = [], []
    def emit(name, data):
        assert data['request_id'] == 'req-test'
        assert data['conversation_id'] == 'conv-test'
        emitted.append((name, data))
    broker = SimpleNamespace(emit=emit)
    job = module.ChatJob(broker, 'req-test', 'conv-test', 'hello', {}, False)
    job.budget = module.STREAM_SECONDS
    job.deadline = job.budget
    frames = iter(frames)

    def recv():
        elapsed, event = next(frames)
        clock.now += elapsed
        if event is None:
            raise module.BrokerError('transport_timeout', 'test read deadline')
        return 'text', json.dumps(event).encode(), None

    job.ws = SimpleNamespace(recv=recv, send_json=sent.append)
    return module, job, emitted, sent


def test_question_pauses_clock_and_result_resumes_it(monkeypatch):
    module, job, emitted, sent = make_job(monkeypatch, [
        (70, {'type': 'question', 'question_id': 'q1', 'question': 'Which?'}),
        (78, {'type': 'tool_activity', 'tool': 'ask_operator', 'phase': 'result'}),
        (200, {'type': 'chunk', 'content': 'still working'}),
        (29, {'type': 'message_complete', 'message_id': 'done'}),
    ])
    assert module.STREAM_SECONDS == 300
    job._read_loop()
    assert job.deadline == 378  # exactly 78 seconds excluded
    assert job.question_until is None
    assert emitted[-1][0] == 'complete'
    assert not sent


def test_resumed_question_uses_remaining_active_budget(monkeypatch):
    _, job, emitted, sent = make_job(monkeypatch, [
        (70, {'type': 'question', 'question_id': 'q1'}),
        (78, {'type': 'tool_activity', 'tool': 'ask_operator', 'phase': 'result'}),
        (230, {'type': 'chunk', 'content': 'too late'}),
    ])
    job._read_loop()
    assert emitted[-1] == ('stopped', {'request_id': 'req-test', 'conversation_id': 'conv-test',
                                      'code': 'stream_deadline'})
    assert sent == [{'type': 'stop_generation', 'conversation_id': 'conv-test'}]


@pytest.mark.parametrize('last_event', [None, {'type': 'chunk', 'content': 'late'}])
def test_question_cap_cannot_be_extended_by_duplicate_or_unrelated_events(monkeypatch, last_event):
    _, job, emitted, sent = make_job(monkeypatch, [
        (70, {'type': 'question', 'question_id': 'q1'}),
        (300, {'type': 'question', 'question_id': 'q1'}),
        (299, {'type': 'tool_activity', 'tool': 'other', 'phase': 'result'}),
        (1, last_event),
    ])
    job._read_loop()
    assert job.question_until == 670
    assert job.ws.read_deadline == 670
    assert emitted[-1][1]['code'] == 'question_unanswered'
    assert sent


def test_question_and_approval_waits_do_not_double_count(monkeypatch):
    _, job, emitted, _ = make_job(monkeypatch, [
        (50, {'type': 'question', 'question_id': 'q1'}),
        (10, {'type': 'builder_wait', 'waiting': True, 'expires_at': 10660}),
        (20, {'type': 'tool_activity', 'tool': 'ask_operator', 'phase': 'result'}),
        (30, {'type': 'builder_wait', 'waiting': False}),
        (200, {'type': 'message_complete'}),
    ])
    job._read_loop()
    assert job.deadline == 360
    assert emitted[-1][0] == 'complete'


def test_approval_pause_and_absolute_expiry_still_apply(monkeypatch):
    _, job, emitted, _ = make_job(monkeypatch, [
        (50, {'type': 'builder_wait', 'waiting': True, 'expires_at': 10650}),
        (599, {'type': 'builder_wait', 'waiting': True, 'expires_at': 11249}),
        (1, None),
    ])
    job._read_loop()
    assert emitted[-1][1]['code'] == 'approval_expired'


def test_ping_traffic_cannot_hold_socket_past_read_deadline(monkeypatch):
    module, _, _, _ = make_job(monkeypatch, [])
    ticks = iter([0, 601])
    module.time.monotonic = lambda: next(ticks)
    ws = object.__new__(module.RawWebSocket)
    ws.sock = Mock()
    ws.max_frame = module.MAX_REPLY_BYTES
    ws._buffer = b'\x89\x00'  # empty ping; after pong, the cap must be checked again
    ws.read_deadline = 600
    ws._send_frame = Mock()
    with pytest.raises(module.BrokerError, match='transport_timeout'):
        ws.recv()
    ws._send_frame.assert_called_once_with(0xA, b'')
    ws.sock.recv.assert_not_called()


def test_partial_frame_reads_cannot_extend_absolute_deadline(monkeypatch):
    module, _, _, _ = make_job(monkeypatch, [])
    ticks = iter([0, 599, 601])
    module.time.monotonic = lambda: next(ticks)
    ws = object.__new__(module.RawWebSocket)
    ws.sock = Mock()
    ws.sock.recv.side_effect = [b'\x81', b'\x00']
    ws._buffer = b''
    ws.read_deadline = 600
    with pytest.raises(module.BrokerError, match='transport_timeout'):
        ws._read_exact(2)
    assert ws.sock.recv.call_count == 2


@pytest.fixture
def live_jobs(monkeypatch):
    """Two actual job threads/sockets, with explicit frames and a shared fake clock."""
    module = load_broker(BACKEND / 'portal-broker/portal-broker.py', monkeypatch)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: 10000+clock.now))
    events, calls, sockets, jobs, threads = {}, [], {}, {}, []

    class Socket:
        def __init__(self):
            self.incoming, self.sent = queue.Queue(), []
            self.closed = False

        def send_json(self, frame):
            self.sent.append(frame)

        def recv(self):
            event = self.incoming.get(timeout=5)
            return 'text', json.dumps(event).encode(), None

        def close(self):
            self.closed = True

    transport = SimpleNamespace(
        rest=lambda *args, **kwargs: (calls.append((args, kwargs)) or {'resolved': True}),
        ws_connect=lambda *args, **kwargs: sockets[threading.current_thread().name])
    broker = module.Broker(transport, {'sub': 'fixture-owner'}, SimpleNamespace(
        event=lambda name, data: events[data['conversation_id']].put((name, data))))
    monkeypatch.setattr(broker, 'conversation_pins', lambda conversation: {})

    def receive(conversation, expected):
        name, data = events[conversation].get(timeout=5)
        assert name == expected
        assert data['conversation_id'] == conversation
        assert data['request_id'] == 'req-' + conversation
        return data

    for conversation in ('a', 'b'):
        sockets[conversation], events[conversation] = Socket(), queue.Queue()
        job = module.ChatJob(broker, 'req-' + conversation, conversation, 'hello', {}, False)
        jobs[conversation] = job
        broker._register(job)
        thread = threading.Thread(target=job.run, name=conversation)
        threads.append(thread)
        thread.start()
        receive(conversation, 'pins')
    try:
        yield SimpleNamespace(module=module, clock=clock, broker=broker, jobs=jobs,
                              sockets=sockets, receive=receive, calls=calls)
    finally:
        for socket in sockets.values():
            socket.incoming.put({'type': 'message_complete'})
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_stop_a_leaves_b_live(live_jobs):
    live = live_jobs
    assert live.broker.op_chat_stop({'conversation_id': 'a'})['stopped'] is True
    assert live.sockets['a'].sent[-1] == {'type': 'stop_generation', 'conversation_id': 'a'}
    assert live.sockets['b'].sent == [{'type': 'message', 'conversation_id': 'b', 'content': 'hello', 'request_id': 'req-b'}]
    live.sockets['a'].incoming.put({'type': 'generation_stopped'})
    live.receive('a', 'stopped')
    assert not live.jobs['b'].finished.is_set()
    live.sockets['b'].incoming.put({'type': 'chunk', 'content': 'B still working'})
    assert live.receive('b', 'chunk')['content'] == 'B still working'
    live.sockets['b'].incoming.put({'type': 'message_complete'})
    live.receive('b', 'complete')


def test_answer_a_resumes_only_a_budget(live_jobs):
    live = live_jobs
    for conversation in ('a', 'b'):
        live.sockets[conversation].incoming.put({'type': 'question', 'question_id': 'q-' + conversation})
        live.receive(conversation, 'question')
    live.clock.now = 40
    assert live.broker.op_chat_answer({'question_id': 'q-a', 'answer': 'yes'})['resolved'] is True
    assert live.calls[-1][0] == ('POST', '/chat/questions/q-a/answer')
    live.sockets['a'].incoming.put({'type': 'tool_activity', 'tool': 'ask_operator', 'phase': 'result'})
    live.receive('a', 'tool_activity')
    live.sockets['b'].incoming.put({'type': 'question', 'question_id': 'q-b'})
    live.receive('b', 'question')
    live.clock.now = 45
    for conversation in ('a', 'b'):
        live.sockets[conversation].incoming.put({'type': 'chunk', 'content': 'tick'})
        live.receive(conversation, 'chunk')
    assert live.jobs['a'].question_until is None
    assert live.jobs['a'].deadline == 340
    assert live.jobs['b'].question_until == 600
    assert live.jobs['b'].deadline == 345


def test_a_deadline_leaves_b_live(live_jobs):
    live = live_jobs
    live.jobs['a'].deadline = 10
    live.clock.now = 10
    live.sockets['a'].incoming.put({'type': 'chunk', 'content': 'late A'})
    assert live.receive('a', 'stopped')['code'] == 'stream_deadline'
    assert live.sockets['a'].sent[-1] == {'type': 'stop_generation', 'conversation_id': 'a'}
    assert not live.jobs['b'].finished.is_set()
    live.sockets['b'].incoming.put({'type': 'chunk', 'content': 'live B'})
    assert live.receive('b', 'chunk')['content'] == 'live B'
    live.sockets['b'].incoming.put({'type': 'message_complete'})
    live.receive('b', 'complete')


def test_second_send_same_conversation_keeps_separate_jobs_and_stops_latest(monkeypatch):
    module = load_broker(BACKEND / 'portal-broker/portal-broker.py', monkeypatch)
    runners = []
    broker = module.Broker(SimpleNamespace(rest=lambda *args, **kwargs: {}),
                           {'sub': 'owner'}, SimpleNamespace(), spawn=runners.append)
    first = broker.op_chat_send({'conversation_id': 'same', 'content': 'first'})
    second = broker.op_chat_send({'conversation_id': 'same', 'content': 'second'})
    assert first.value['accepted'] is True and second.value['accepted'] is True
    assert first.value['request_id'] != second.value['request_id']
    assert len(runners) == 2
    old, replacement = broker._jobs['same']
    assert broker._active_job('same') is replacement
    assert broker.op_chat_stop({'conversation_id': 'same'})['stopped'] is True
    assert replacement.stop_requested and not old.stop_requested
    broker.forget(old)
    assert broker._active_job('same') is replacement


def test_all_job_frame_types_have_both_ids(monkeypatch):
    _, job, emitted, _ = make_job(monkeypatch, [
        (0, {'type': 'builder_wait', 'waiting': True, 'expires_at': 10600, 'agent': 'builder'}),
        (0, {'type': 'builder_wait', 'waiting': False, 'agent': 'builder'}),
        (0, {'type': 'chunk', 'content': 'text'}),
        (0, {'type': 'question', 'question_id': 'q'}),
        (0, {'type': 'tool_activity', 'tool': 'ask_operator', 'phase': 'result'}),
        (0, {'type': 'approval_request', 'approval_id': 'approval', 'action': 'write', 'details': {}}),
        (0, {'type': 'error', 'code': 'nonfatal', 'message': 'still running'}),
        (0, {'type': 'message_complete', 'message_id': 'done'}),
    ])
    job._emit('pins', {'face_id': 'builder'})
    job._read_loop()
    job._terminal('stopped')
    assert {name for name, _ in emitted} == {
        'pins', 'builder_wait', 'chunk', 'question', 'tool_activity',
        'approval_request', 'error', 'complete', 'stopped'}
    for _, data in emitted:
        assert data['request_id'] == 'req-test'
        assert data['conversation_id'] == 'conv-test'
    assert emitted[1][1]['agent'] == 'builder'


@pytest.mark.parametrize('reason', [None, 'too_many_active_conversations'])
def test_concurrency_refusal_keeps_text_and_code_and_tags_reason(monkeypatch, reason):
    event = {'type': 'error', 'code': 'concurrency_limit', 'message': 'Too many concurrent conversations'}
    if reason:
        event['reason'] = reason
    _, job, emitted, _ = make_job(monkeypatch, [(0, event)])
    job._read_loop()
    assert [name for name, _ in emitted] == ['error', 'stopped']
    assert emitted[0][1]['message'] == event['message']
    for _, data in emitted:
        assert data == {**data, 'request_id': 'req-test', 'conversation_id': 'conv-test',
                        'code': 'concurrency_limit', 'reason': 'too_many_active_conversations'}


@pytest.mark.parametrize('failure', ['broker', 'internal', 'connect'])
def test_exception_paths_tag_error_and_terminal(monkeypatch, failure):
    module, job, emitted, _ = make_job(monkeypatch, [])
    job.broker.forget = Mock()
    job.ws.close = Mock()
    if failure == 'connect':
        job.broker.conversation_pins = lambda conversation: {}
        job.broker.stream_seconds, job.broker.chat_idle = 300, 900
        job.broker.transport = SimpleNamespace(ws_connect=Mock(side_effect=module.BrokerError('transport_unavailable')))
    else:
        monkeypatch.setattr(job, '_run', Mock(side_effect=(
            module.BrokerError('test_failure') if failure == 'broker' else ValueError('private detail'))))
    job.run()
    assert [name for name, _ in emitted] == ['error', 'stopped']
    for _, data in emitted:
        assert data['request_id'] == 'req-test'
        assert data['conversation_id'] == 'conv-test'
    assert 'private detail' not in str(emitted)
    job.broker.forget.assert_called_once_with(job)
