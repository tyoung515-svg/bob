"""Task-end uses the real execution-vouch registry, not a mocked verifier."""
import time
import pytest
from core.permissions import execution_vouch,verify_execution_vouch


@pytest.fixture(autouse=True)
def mock_redis():
    yield


def test_task_end_is_explicitly_registered_in_the_signed_execution_protocol():
    now=int(time.time())
    envelope={'operation':'builder.task.end','args':{'conversation_id':'owned'},
              'authority':{'owner':'owner'},'iat':now,'exp':now+30}
    secret='c6-fixture-execution-secret'
    assert verify_execution_vouch(envelope,execution_vouch(envelope,secret),secret)
    changed={**envelope,'args':{'conversation_id':'someone-else'}}
    assert not verify_execution_vouch(changed,execution_vouch(envelope,secret),secret)
    changed={**envelope,'operation':'builder.task.raise_authority'}
    assert not verify_execution_vouch(changed,execution_vouch(changed,secret),secret)
