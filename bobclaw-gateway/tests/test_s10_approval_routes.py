"""S10 requested-mode challenge context through real HTTP and SQLite."""
import uuid
from pathlib import Path

import pytest
from tests.test_owner_sessions import owner_h
from db import _sqlite_connection


@pytest.fixture
def tmp_path():
    # Managed Windows cannot reopen Python 3.13 mode=0700 temporary DACLs.
    path = Path(__file__).parents[1]/'test-db'/('s10-'+uuid.uuid4().hex)
    path.mkdir(mode=0o777,parents=True)
    return path


@pytest.mark.parametrize('mode',['suggest','approve','auto','yolo'])
async def test_requested_mode_is_scoped_and_preserved_in_challenge(owner_h,mode):
    h=owner_h
    resource={'conversation_id':'owned','requested_mode':mode}
    response=await h.client.post('/auth/owner/begin',headers=h.headers,
        json={'operation':'builder.mode.set','resource':resource})
    assert response.status==200,await response.text()
    challenge=await response.json()
    envelope=h.hits[-1]['envelope']
    assert envelope['operation']=='owner.summary'
    assert envelope['args']=={'operation':'builder.mode.set','resource':resource}
    async with _sqlite_connection() as db:
        async with db.execute('SELECT summary FROM owner_challenges WHERE id=?',(challenge['challenge_id'],)) as cursor:
            import json
            assert json.loads((await cursor.fetchone())[0])==challenge['summary']


@pytest.mark.parametrize('operation,mode', [('builder.approve','auto'),('builder.mode.set','invalid'),('builder.mode.set',True)])
async def test_requested_mode_never_confers_other_operation_authority(owner_h,operation,mode):
    h=owner_h
    before=len(h.hits)
    response=await h.client.post('/auth/owner/begin',headers=h.headers,
        json={'operation':operation,'resource':{'conversation_id':'owned','requested_mode':mode}})
    assert response.status==422,await response.text()
    assert len(h.hits)==before
