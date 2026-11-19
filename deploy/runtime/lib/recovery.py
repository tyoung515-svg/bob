"""Root-private recovery challenges. The generation pointer commits every reset.

Only the mapped gateway can call these operations. Factor assertions from that
UID are bound to a context, epoch and short-lived challenge; stick proof is
independently checked here. No recovery secret is ever persisted.
"""
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timezone

from core.recovery_contract import digest
from core.settings_contract import SettingsError

TTL = 300


def binding(cfg):
    return {k: cfg[k] for k in ('installation_id', 'lane_id', 'owner')}


def pending(cfd):
    from settings import load
    try:
        value = load(cfd, 'pending.json')
    except FileNotFoundError:
        return None
    if value['expires_at'] <= time.time():
        clear(cfd)
        return None
    return value


def clear(cfd):
    try:
        os.unlink('pending.json', dir_fd=cfd)
        os.fsync(cfd)
    except FileNotFoundError:
        pass


def limited(cfd):
    from settings import load
    try:
        state = load(cfd, 'factor-attempts.json')
    except FileNotFoundError:
        return
    if state['next_at'] > time.time():
        raise SettingsError('authentication_throttled')


def failed(cfd):
    from settings import load, publish
    try:
        state = load(cfd, 'factor-attempts.json')
    except FileNotFoundError:
        state = {'failures': 0}
    count = min(16, state['failures'] + 1)
    publish(cfd, 'factor-attempts.json', {'failures': count,
            'next_at': time.time() + min(300, 2 ** (count - 1))})


def prepare(cfg, cfd, request):
    from settings import load, publish
    limited(cfd)
    active = load(cfd, 'active.json')
    previous = load(cfd, active['generation'] + '.json')
    pair = request['pair']
    if active['epoch'] != request['expected_epoch']:
        raise SettingsError('invalid_factors')
    if pending(cfd):
        raise SettingsError('recovery_busy')
    try:
        if pair == 'setup':
            try:
                load(cfd, 'setup-used.json')
            except FileNotFoundError:
                pass
            else:
                raise SettingsError('invalid_factors')
            cap = hashlib.sha256(request['setup_capability'].encode()).hexdigest()
            if (previous.get('totp_enrolled') or previous.get('recovery_hash')
                    or not previous.get('setup_hash') or not hmac.compare_digest(cap, previous['setup_hash'])):
                raise SettingsError('invalid_factors')
        elif not previous.get('totp_enrolled'):
            raise SettingsError('invalid_factors')
        if 'old_stick' in request:
            old = request['old_stick']
            if (any(old[k] != v for k, v in binding(cfg).items())
                    or old['recovery_id'] != previous.get('recovery_id')
                    or not previous.get('recovery_hash')
                    or not hmac.compare_digest(digest(binding(cfg), old['recovery_id'], old['secret']), previous['recovery_hash'])):
                raise SettingsError('invalid_factors')
        replacement = request['replacement']
        if (replacement['recovery_id'] == previous.get('recovery_id')
                or replacement['verifier'] == previous.get('recovery_hash')):
            raise SettingsError('invalid_factors')
        if request.get('new_seed') == previous.get('totp_secret') or request.get('new_password') == previous.get('password'):
            raise SettingsError('invalid_factors')
    except SettingsError:
        failed(cfd)
        raise
    challenge = dict(challenge_id=secrets.token_hex(32), expires_at=int(time.time()) + TTL,
                     state='proof_verified', epoch=active['epoch'], context=request['context'],
                     pair=pair, replacement=dict(replacement))
    for key in ('new_seed', 'new_password'):
        if key in request:
            challenge[key] = request[key]
    # One lane/epoch claim, even for the no-stick branch. No old secret or proof
    # is written. Expiry releases the claim but cannot unspend gateway proof.
    if pair == 'setup':
        publish(cfd, 'setup-used.json', {'consumed': True})
    publish(cfd, 'pending.json', challenge)
    return {k: challenge[k] for k in ('challenge_id', 'expires_at', 'state')}


def challenge(cfd, request):
    from settings import load
    value = pending(cfd)
    active = load(cfd, 'active.json')
    if (not value or value['challenge_id'] != request['challenge_id']
            or value['context'] != request['context']
            or value['epoch'] != request['expected_epoch'] or value['epoch'] != active['epoch']):
        raise SettingsError('invalid_factors')
    return value, active


def commit(cfg, fd, cfd, request, receipt):
    from settings import load, publish
    from credentials import checkpoint, environment, recover
    limited(cfd)
    try:
        value, active = challenge(cfd, request)
        floor = None
        if 'new_seed' in value:
            import pyotp
            generator = pyotp.TOTP(value['new_seed'])
            step = int(time.time() / 30)
            for candidate in range(step - 1, step + 2):
                if hmac.compare_digest(request['new_totp'], generator.at(candidate * 30)):
                    floor = candidate
                    break
            if floor is None:
                raise SettingsError('invalid_factors')
        elif request['new_totp']:
            raise SettingsError('invalid_args')
        if request['material_saved'] is not True:
            raise SettingsError('invalid_args')
    except SettingsError:
        failed(cfd)
        raise
    previous = load(cfd, active['generation'] + '.json')
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    event = dict(operation_id=receipt['operation_id'], operation='enrollment' if value['pair'] == 'setup' else 'recovery',
                 factor_pair=value['pair'], actor=cfg['owner'], source_class='owner_portal',
                 reset_at=now, epoch=active['epoch'] + 1)
    generation = {**previous, 'epoch': event['epoch'], 'recovery_id': value['replacement']['recovery_id'],
                  'recovery_hash': value['replacement']['verifier'], 'totp_enrolled': True,
                  'setup_hash': None, 'reset_at': now, 'revocation_epoch': event['epoch'], 'audit': event}
    if 'new_seed' in value:
        generation.update(totp_secret=value['new_seed'], totp_floor=floor)
    if 'new_password' in value:
        generation['password'] = value['new_password']
    value['state'] = 'material_saved'
    publish(cfd, 'pending.json', value)
    checkpoint('prepared')
    publish(cfd, receipt['operation_id'] + '.json', generation)
    checkpoint('generation')
    publish(cfd, 'current.env', environment(generation))
    checkpoint('env')
    publish(cfd, 'active.json', dict(epoch=generation['epoch'], generation=receipt['operation_id'],
            operation_id=receipt['operation_id'], reset_at=now, revocation_epoch=generation['epoch']), mode=0o644)
    checkpoint('pointer')
    # Audit is part of the immutable committed generation and cannot be lost if
    # a crash happens before the separate, safe security-history projection.
    return recover(cfg, fd, cfd, receipt, resume=True)['result']


def finish(cfg, fd, cfd, receipt, *, publish_portal):
    from settings import load, publish
    active = load(cfd, 'active.json')
    if active.get('operation_id') != receipt['operation_id']:
        return
    generation = load(cfd, active['generation'] + '.json')
    publish(fd, 'audit-' + receipt['operation_id'] + '.json', generation['audit'])
    if publish_portal:
        # Startup may retry an older failed portal handoff after a new challenge
        # was prepared. Only the claim spent by this generation may be removed.
        try:
            claim = load(cfd, 'pending.json')
        except FileNotFoundError:
            pass
        else:
            if claim['epoch'] < active['epoch']:
                clear(cfd)
    if publish_portal and not receipt.get('portal_published'):
        # Runtime publisher writes the replacement directly to bobapp's private
        # token file. Neither helper replies nor the desktop receive a bearer.
        from lane import rotate_token
        rotate_token(cfg['lane'])
        receipt['portal_published'] = True
