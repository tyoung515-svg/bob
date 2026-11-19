"""Root/Linux real journal, claim races and recovery verifier persistence."""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pyotp
import pytest

from test_password_journal import credentials, PowerLoss
from test_settings import helper
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'bobclaw-core'))
from core.recovery_contract import digest  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == 'nt' or getattr(os,'geteuid',lambda:-1)()!=0,
                               reason='requires root Linux real fsync/no-follow/flock')


@pytest.fixture
def recovery_h(credentials,monkeypatch):
    m,c,cfg,home,loaded,restarts=credentials
    cfg.update(installation_id='install',owner='owner')
    spec=importlib.util.spec_from_file_location('recovery',Path(m.__file__).with_name('recovery.py'))
    r=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,'recovery',r)
    spec.loader.exec_module(r)
    publications=[]
    monkeypatch.setitem(sys.modules,'lane',SimpleNamespace(rotate_token=lambda lane:publications.append(lane)))
    old=base64.b32encode(b'x'*32).decode().rstrip('=')
    replacement=base64.b32encode(b'y'*32).decode().rstrip('=')
    with m.directory(home/'credentials') as fd:
        generation=m.load(fd,'initial.json')
        generation.update(recovery_hash=digest(r.binding(cfg),'old',old),recovery_id='old')
        m.publish(fd,'initial.json',generation)
    return SimpleNamespace(m=m,c=c,cfg=cfg,home=home,loaded=loaded,restarts=restarts,r=r,
        publications=publications,old=old,replacement=replacement)


def prepare(h,pair='password_stick',key='prepare',epoch=1):
    value=dict(version=1,op='factors.prepare',request_key=key,expected_epoch=epoch,context='c'*64,pair=pair,
        replacement=dict(recovery_id='new',verifier=digest(h.r.binding(h.cfg),'new',h.replacement)))
    if pair in {'password_stick','totp_stick'}:
        value['old_stick']={**h.r.binding(h.cfg),'recovery_id':'old','secret':h.old}
    if pair in {'password_stick','setup'}:
        value['new_seed']='A'*32
    if pair=='totp_stick':
        value['new_password']='replacement-password-canary'
    if pair=='setup':
        value['setup_capability']='CAPABILITY-CANARY'
    return value


def commit(h,challenge,key='commit',**changes):
    return dict(version=1,op='factors.commit',request_key=key,expected_epoch=1,context='c'*64,
        challenge_id=challenge['challenge_id'],material_saved=True,
        new_totp=pyotp.TOTP('A'*32).now(),**changes)


def call(h,value):
    return h.m.handle(h.cfg,h.cfg['gateway_uid'],value)


def status(h,key):
    return h.m.handle(h.cfg,h.cfg['broker_uid'],dict(version=1,op='status',request_key=key),readonly=True)


@pytest.mark.parametrize('pair',['password_stick','password_totp','totp_stick'])
def test_each_pair_one_atomic_generation_fresh_stick_audit_and_copied_old_rejected(recovery_h,pair):
    h=recovery_h
    challenge=call(h,prepare(h,pair))
    value=commit(h,challenge)
    if pair!='password_stick':
        value['new_totp']=''
    assert call(h,value)==dict(changed=True,restarted=True)
    active=json.loads((h.home/'credentials/active.json').read_text())
    generation=json.loads((h.home/'credentials'/ (active['generation']+'.json')).read_text())
    assert active['epoch']==active['revocation_epoch']==generation['epoch']==2
    assert generation['recovery_id']=='new' and generation['recovery_hash']==digest(h.r.binding(h.cfg),'new',h.replacement)
    assert generation['password']==('replacement-password-canary' if pair=='totp_stick' else 'old-password-canary')
    assert generation['totp_secret']==('A'*32 if pair=='password_stick' else 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP')
    assert generation['audit']['factor_pair']==pair and active['reset_at']==generation['audit']['reset_at']
    assert len(h.publications)==len(h.restarts)==1
    assert not (h.home/'credentials/pending.json').exists()
    with pytest.raises(h.m.SettingsError,match='invalid_factors'):
        call(h,prepare(h,key='copied-old',epoch=2))
    for path in h.home.glob('*.json'):
        raw=path.read_text()
        assert all(secret not in raw for secret in [h.old,h.replacement,'A'*32,'replacement-password-canary'])
    assert all(h.old not in p.read_text() and h.replacement not in p.read_text() for p in (h.home/'credentials').glob('*.json'))


@pytest.mark.parametrize('field',['installation_id','lane_id','owner','recovery_id','secret'])
def test_mixed_lane_and_wrong_stick_refused_without_claim(recovery_h,field):
    h=recovery_h
    value=prepare(h)
    value['old_stick'][field]='A'*52 if field=='secret' else 'foreign'
    with pytest.raises(h.m.SettingsError,match='invalid_factors'):
        call(h,value)
    assert not (h.home/'credentials/pending.json').exists()
    assert json.loads((h.home/'credentials/active.json').read_text())['epoch']==1
    with pytest.raises(h.m.SettingsError,match='authentication_throttled'):
        call(h,prepare(h,key='nonce-cycle'))


def test_simultaneous_stick_claim_exactly_one_winner(recovery_h):
    h=recovery_h
    def attempt(key):
        try:
            return call(h,prepare(h,key=key))
        except h.m.SettingsError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(attempt,['one','two']))
    assert sum(isinstance(result,dict) for result in results)==1
    assert results.count('recovery_busy')==1
    result=next(r for r in results if isinstance(r,dict))
    with pytest.raises(h.m.SettingsError,match='request_key_used'):
        call(h,prepare(h,key='one'))
    assert call(h,commit(h,result))['changed']
    # Even a copied challenge cannot commit under a second operation key.
    assert call(h,commit(h,result,key='copy'))==dict(changed=False,restarted=False)


@pytest.mark.parametrize('boundary',['prepared','generation','env','pointer','committed','restart-pending','restarted','terminal'])
def test_crash_before_after_commit_receipt_and_no_credential_replay(recovery_h,monkeypatch,boundary):
    h=recovery_h
    challenge=call(h,prepare(h))
    def crash(name):
        if name==boundary:
            raise PowerLoss()
    monkeypatch.setattr(h.c,'checkpoint',crash)
    with pytest.raises(PowerLoss):
        call(h,commit(h,challenge))
    monkeypatch.setattr(h.c,'checkpoint',lambda name:None)
    before=list(h.restarts)
    h.m.repair(h.cfg)
    assert h.restarts==before
    h.m.recover_all(h.cfg)
    committed=boundary in {'pointer','committed','restart-pending','restarted','terminal'}
    assert status(h,'commit')['result']==dict(changed=committed,restarted=committed)
    active=json.loads((h.home/'credentials/active.json').read_text())
    assert active['epoch']==(2 if committed else 1)
    generation=json.loads((h.home/'credentials'/(active['generation']+'.json')).read_text())
    assert (h.home/'credentials/current.env').read_text()==h.c.environment(generation)
    if committed:
        assert len(list(h.home.glob('audit-*.json')))==1
    with pytest.raises(h.m.SettingsError,match='request_key_used'):
        call(h,commit(h,challenge))


def test_expiry_cancel_and_failed_material_save_keep_old_factors(recovery_h,monkeypatch):
    h=recovery_h
    challenge=call(h,prepare(h))
    value=commit(h,challenge)
    value['material_saved']=False
    with pytest.raises(h.m.SettingsError,match='invalid_args'):
        call(h,value)
    with h.m.directory(h.home/'credentials') as fd:
        pending=h.m.load(fd,'pending.json')
        pending['expires_at']=0
        h.m.publish(fd,'pending.json',pending)
    assert call(h,commit(h,challenge))==dict(changed=False,restarted=False)
    monkeypatch.setattr(h.r,'limited',lambda fd:None)
    challenge=call(h,prepare(h,key='fresh-claim'))
    assert call(h,dict(version=1,op='factors.cancel',request_key='cancel',expected_epoch=1,
                     context='c'*64,challenge_id=challenge['challenge_id']))==dict(changed=False,restarted=False)
    assert not (h.home/'credentials/pending.json').exists()
    assert json.loads((h.home/'credentials/active.json').read_text())['epoch']==1


def test_setup_requires_single_use_provisioned_capability_and_new_seed_proof(recovery_h):
    h=recovery_h
    with h.m.directory(h.home/'credentials') as fd:
        initial=h.m.load(fd,'initial.json')
        initial.update(totp_enrolled=False,totp_secret='',recovery_id=None,recovery_hash=None,
            setup_hash=hashlib.sha256(b'CAPABILITY-CANARY').hexdigest())
        h.m.publish(fd,'initial.json',initial)
    challenge=call(h,prepare(h,'setup'))
    assert call(h,commit(h,challenge))['changed']
    assert status(h,'prepare')['result']==challenge
    assert (h.home/'credentials/setup-used.json').exists()
    assert json.loads((h.home/'credentials/active.json').read_text())['revocation_epoch']==2


def test_wrong_new_seed_proof_context_and_readonly_receipt_do_not_mutate_factors(recovery_h,monkeypatch):
    h=recovery_h
    challenge=call(h,prepare(h))
    value=commit(h,challenge)
    value['context']='d'*64
    assert call(h,value)==dict(changed=False,restarted=False)
    monkeypatch.setattr(h.r,'limited',lambda fd:None)
    value=commit(h,challenge,key='wrong-totp')
    value['new_totp']=''  # no valid replacement authenticator proof
    assert call(h,value)==dict(changed=False,restarted=False)
    assert json.loads((h.home/'credentials/active.json').read_text())['epoch']==1
    with pytest.raises(h.m.SettingsError):
        h.m.handle(h.cfg,h.cfg['broker_uid'],prepare(h),readonly=True)


def test_restart_failure_still_revokes_and_receipt_cannot_publish_or_restart(recovery_h,monkeypatch):
    h=recovery_h
    challenge=call(h,prepare(h))
    monkeypatch.setattr(h.c,'restart',lambda *a:False)
    assert call(h,commit(h,challenge))==dict(changed=True,restarted=False)
    before={p.name:p.stat().st_mtime_ns for p in (h.home/'credentials').iterdir()}
    publications=list(h.publications)
    assert status(h,'commit')['result']==dict(changed=True,restarted=False)
    assert before=={p.name:p.stat().st_mtime_ns for p in (h.home/'credentials').iterdir()}
    assert publications==h.publications


@pytest.mark.parametrize('pair',['password_stick','password_totp','totp_stick'])
def test_publisher_failure_cannot_strand_gateway_or_receipt(recovery_h,monkeypatch,pair):
    h=recovery_h
    challenge=call(h,prepare(h,pair))
    value=commit(h,challenge)
    if pair!='password_stick':
        value['new_totp']=''
    attempts=[]
    def denied(lane):
        # Model ProtectSystem=strict denying the private publisher destination.
        assert h.loaded[0]==2 and h.restarts==[(h.cfg['lane'],2)]
        attempts.append(lane)
        raise OSError(30,'Read-only file system: SECRET-CANARY')
    monkeypatch.setattr(sys.modules['lane'],'rotate_token',denied)
    assert call(h,value)==dict(changed=True,restarted=False)
    receipt=status(h,'commit')
    assert receipt==dict(operation_id=receipt['operation_id'],phase='failed',
                         result=dict(changed=True,restarted=False))
    journal=json.loads((h.home/('op-'+receipt['operation_id']+'.json')).read_text())
    assert journal['gateway_ready_epoch']==2
    assert journal['failure']=='portal_publication_failed'
    assert 'SECRET-CANARY' not in json.dumps(journal)
    assert len(attempts)==1  # Neither exception reconciliation nor status replayed it.
    monkeypatch.setattr(sys.modules['lane'],'rotate_token',lambda lane:h.publications.append(lane))
    # A later control startup may retry the handoff, preserving terminal history
    # and a fresh claim prepared against the now-loaded generation.
    fresh=prepare(h,'password_totp',key='fresh',epoch=2)
    fresh['replacement']=dict(recovery_id='next',verifier='b'*64)
    fresh_challenge=call(h,fresh)
    h.m.recover_all(h.cfg)
    assert h.publications==[h.cfg['lane']]
    assert h.restarts==[(h.cfg['lane'],2)]
    assert status(h,'commit')==receipt
    pending=json.loads((h.home/'credentials/pending.json').read_text())
    assert pending['challenge_id']==fresh_challenge['challenge_id']


@pytest.mark.parametrize('pair',['password_stick','password_totp','totp_stick'])
def test_commit_restarts_only_mapped_gateway_and_publishes_after_loaded_readiness(recovery_h,monkeypatch,pair):
    h=recovery_h
    # Exercise the actual restart function rather than the fixture's substitute.
    spec=importlib.util.spec_from_file_location('restart_credentials',Path(h.c.__file__))
    native=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    monkeypatch.setattr(native,'ready',h.c.ready)
    calls=[]
    def systemctl(argv,**kwargs):
        assert argv==['/usr/bin/systemctl','restart',f'bob-gateway@{h.cfg["lane"]}.service']
        assert kwargs['check'] and kwargs['timeout']==45
        calls.append(argv)
        h.loaded[0]=2
    monkeypatch.setattr(native.subprocess,'run',systemctl)
    monkeypatch.setattr(h.c,'restart',native.restart)
    def publish(lane):
        assert lane==h.cfg['lane'] and h.loaded[0]==2 and len(calls)==1
        h.publications.append(lane)
    monkeypatch.setattr(sys.modules['lane'],'rotate_token',publish)
    challenge=call(h,prepare(h,pair))
    value=commit(h,challenge)
    if pair!='password_stick':
        value['new_totp']=''
    assert call(h,value)==dict(changed=True,restarted=True)
    receipt=status(h,'commit')
    assert receipt['phase']=='ready'
    journal=json.loads((h.home/('op-'+receipt['operation_id']+'.json')).read_text())
    assert journal['gateway_ready_epoch']==2 and journal['portal_published'] is True
    h.m.recover_all(h.cfg)
    assert len(calls)==len(h.publications)==1


def test_readonly_cannot_finish_ready_before_portal_publication(recovery_h,monkeypatch):
    h=recovery_h
    challenge=call(h,prepare(h))
    def crash(name):
        if name=='restarted':
            raise PowerLoss()
    monkeypatch.setattr(h.c,'checkpoint',crash)
    with pytest.raises(PowerLoss):
        call(h,commit(h,challenge))
    assert h.loaded[0]==2 and not h.publications
    receipt=status(h,'commit')
    assert receipt['phase']=='restart-pending' and 'result' not in receipt
    assert not h.publications and len(h.restarts)==1
    monkeypatch.setattr(h.c,'checkpoint',lambda name:None)
    h.m.recover_all(h.cfg)
    assert status(h,'commit')['phase']=='ready'
    assert len(h.publications)==len(h.restarts)==1


def test_real_portal_publisher_uses_committed_env_without_writing_readonly_lane(recovery_h,monkeypatch):
    h=recovery_h
    spec=importlib.util.spec_from_file_location('publisher_lane',Path(h.m.__file__).with_name('lane.py'))
    lane=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lane)
    import common
    import worker
    monkeypatch.setattr(common,'execution_mode',lambda:'worker')
    monkeypatch.setattr(worker,'load_identity',lambda name:h.cfg)
    monkeypatch.setattr(lane,'FAMILY',h.home/'family')
    monkeypatch.setattr(lane,'PORTAL',h.home/'portal')
    monkeypatch.setattr(lane,'DATA',h.home/'data')
    monkeypatch.setattr(lane,'broker_scope',lambda name:{})
    monkeypatch.setattr(lane.pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=1005,pw_gid=1005))
    lane_root=lane.FAMILY/h.cfg['lane']/'bob-memory'
    lane_root.mkdir(parents=True)
    (lane.DATA/'secrets').mkdir(parents=True)
    (lane.DATA/'secrets/bobclaw.env').write_text('')
    (lane_root/'lane.env').write_text('JWT_SECRET="synthetic"\nBOBCLAW_GATEWAY_PORT="7836"\n')
    original_unlink=Path.unlink
    def readonly_unlink(path,*args,**kwargs):
        if path==lane_root/'broker.token':
            raise OSError(30,'Read-only file system')
        return original_unlink(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',readonly_unlink)
    def mint(argv,**kwargs):
        assert h.loaded[0]==2
        assert kwargs['env']['BOB_CREDENTIAL_EPOCH']=='2'
        assert kwargs['env']['BOB_SETTINGS_ID']==h.cfg['lane_id']
        assert kwargs['env']['BOBCLAW_PASSWORD']=='old-password-canary'
        # The real publisher consumes this inherited private fd, never stdout.
        os.write(kwargs['pass_fds'][0],b'synthetic-bearer\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(lane.subprocess,'run',mint)
    monkeypatch.setitem(sys.modules,'lane',lane)
    challenge=call(h,prepare(h,'password_totp'))
    value=commit(h,challenge)
    value['new_totp']=''
    assert call(h,value)==dict(changed=True,restarted=True)
    assert (lane.PORTAL/(h.cfg['lane']+'.token')).read_text()=='synthetic-bearer\n'
    assert json.loads((lane.PORTAL/(h.cfg['lane']+'.json')).read_text())==dict(
        gateway_port=7836,settings_id=h.cfg['lane_id'])
    assert status(h,'commit')['phase']=='ready'


def test_timer_never_renews_revoked_service_epoch(recovery_h,monkeypatch):
    h=recovery_h
    spec=importlib.util.spec_from_file_location('s3g_runtime_lane',Path(h.m.__file__).with_name('lane.py'))
    lane=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lane)
    import jwt
    import common
    import worker
    monkeypatch.setattr(common,'execution_mode',lambda:'worker')
    monkeypatch.setattr(worker,'load_identity',lambda name:h.cfg)
    monkeypatch.setattr(lane,'FAMILY',h.home/'family')
    monkeypatch.setattr(lane,'PORTAL',h.home/'portal')
    monkeypatch.setattr(lane,'rotate_token',lambda name:pytest.fail('must not renew revoked authority'))
    (lane.FAMILY/h.cfg['lane']/'bob-memory').mkdir(parents=True)
    (lane.FAMILY/h.cfg['lane']/'bob-memory/lane.env').write_text('JWT_SECRET="fixture-key"\n')
    lane.PORTAL.mkdir()
    old=jwt.encode({'sub':h.cfg['lane'],'token_type':'portal','service_epoch':0,'exp':9999999999},'fixture-key',algorithm='HS256')
    (lane.PORTAL/(h.cfg['lane']+'.token')).write_text(old)
    with h.m.directory(h.home/'credentials') as fd:
        active=h.m.load(fd,'active.json')
        h.m.publish(fd,'active.json',{**active,'revocation_epoch':2},mode=0o644)
    with pytest.raises(ValueError,match='revoked portal'):
        lane.renew_if_due(h.cfg['lane'])
