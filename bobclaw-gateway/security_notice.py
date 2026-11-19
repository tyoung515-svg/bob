"""Safe reset notice, retained with the root generation's security audit."""
from core import credential_state


def notice():
    if not credential_state.enabled():
        return {}
    value = credential_state.active().get('reset_at')
    if not value:
        return {}
    from datetime import datetime
    date = datetime.fromisoformat(value).date().isoformat()
    return {'security_notice': 'two-factor was reset on ' + date}
