"""Real private transcript/status files; no provider and no credential reads."""
import json
import os
from types import SimpleNamespace
import pytest
from core.backends import claude_state as state

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux inode/durability checks')


@pytest.fixture
def lane(tmp_path, monkeypatch):
    lane = SimpleNamespace(root=tmp_path, values={'CLAUDE_AUTH_GENERATION': 'generation-one'})
    monkeypatch.setattr('core.lane_config.get_lane', lambda: lane)
    return lane


def test_append_only_intent_results_and_usage_are_separate_from_auth(lane):
    turn = 'a'*32
    state.record(turn, 'start', {'selection': 'sonnet'})
    state.record(turn, 'tool_intent', {'name': 'fs_read', 'args': {'path': 'test.txt'}})
    state.record(turn, 'tool_result', {'name': 'fs_read', 'content': 'evidence'})
    state.record(turn, 'model', {'selection': 'sonnet', 'reported': 'claude-full-first'})
    state.record(turn, 'model', {'selection': 'sonnet', 'reported': 'claude-full-later'})
    state.record(turn, 'usage', {'input_tokens': 11, 'output_tokens': 7, 'billing': 'subscription'})
    rows = [json.loads(l) for l in (lane.root/'claude-turns'/(turn+'.jsonl')).read_text().splitlines()]
    assert [r['kind'] for r in rows[:3]] == ['start', 'tool_intent', 'tool_result']
    assert (lane.root/'claude-turns'/(turn+'.jsonl')).stat().st_mode & 0o777 == 0o600
    value = state.load(lane.root, 'generation-one')
    assert value['models']['sonnet'] == 'claude-full-first'
    assert value['last_usage']['billing'] == 'subscription'
    assert 'cost' not in json.dumps(value)
    assert state.load(lane.root, 'generation-two') == {}


def test_signed_out_is_unknown_until_new_auth_generation(lane):
    state.record('a'*32, 'signed_out', {})
    assert state.load(lane.root, 'generation-one')['status'] == 'unknown'
    assert state.load(lane.root, 'generation-two') == {}


def test_symlinked_transcript_is_never_written(lane, tmp_path):
    folder = lane.root/'claude-turns'; folder.mkdir(mode=0o700)
    target = tmp_path/'untouched'; target.write_text('unchanged')
    (folder/('a'*32+'.jsonl')).symlink_to(target)
    with pytest.raises(OSError): state.record('a'*32, 'tool_intent', {})
    assert target.read_text() == 'unchanged'


def test_first_reported_name_is_kept_separately_for_every_alias(lane):
    from core.worker.claude import MODELS
    for index, alias in enumerate(MODELS):
        turn = format(index + 1, '032x')
        state.record(turn, 'model', {'selection': alias, 'reported': 'full-' + alias})
        state.record(turn, 'model', {'selection': alias, 'reported': 'later-' + alias})
    observed = state.load(lane.root, 'generation-one')['models']
    assert observed == {alias: 'full-' + alias for alias in MODELS}
