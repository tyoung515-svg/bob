"""Finite job policy, separate from exact model-call accounting.

Only service adapters create JobPolicy/Reconciliation objects. No public Jobs API
is enabled by this contract. Legacy short tool timeouts keep their old meaning.
"""
from dataclasses import dataclass
import math
import re

from .models import ExecutionError, integer, only, digest

PRESETS = (900, 1800, 3600)
MAX_JOB_SECONDS = 2147483647  # representation bound, not owner authority
DEFAULT_MAX_ATTEMPTS = 32


def seconds(value):
    return integer(value, 1, MAX_JOB_SECONDS)


@dataclass(frozen=True)
class JobPolicy:
    proposed_seconds: int
    rationale: str
    owner_override: int | None = None
    auto_escalate: bool = True
    custom_limit: bool = False

    def __post_init__(self):
        seconds(self.proposed_seconds)
        if not isinstance(self.rationale, str) or not self.rationale.strip() or len(self.rationale) > 2000:
            raise ExecutionError('job_rationale_required')
        if self.owner_override is not None:
            seconds(self.owner_override)
        if type(self.auto_escalate) is not bool or type(self.custom_limit) is not bool:
            raise ExecutionError('invalid_job_policy')

    @property
    def selected_seconds(self):
        return self.owner_override if self.owner_override is not None else self.proposed_seconds


def next_tier(selected):
    # asyncpg reads selected_seconds (DOUBLE PRECISION) as float, even when
    # the selected policy uses whole seconds. Normalize only exact integers;
    # keep seconds() strict for fractional values and the doubled upper bound.
    if type(selected) is float and selected.is_integer():
        selected = int(selected)
    for preset in PRESETS:
        if selected < preset:
            return preset
    return seconds(selected * 2)


def authorize(selected, grant=None, *, explicit=True, continuation=False, force_custom=False):
    """Reject selections before resource reductions; never clip into authority."""
    if type(selected) not in (int, float) or not math.isfinite(selected) or not .1 <= selected <= MAX_JOB_SECONDS:
        raise ExecutionError('invalid_job_limit')
    # The reviewed extended ladder doubles 60 minutes: 120, 240, ... .
    # Other finite selections (or an explicitly custom preset) are custom.
    ratio = selected / 3600
    extended_tier = selected > 3600 and ratio.is_integer() and int(ratio) & (int(ratio)-1) == 0
    custom = force_custom or (selected not in PRESETS and not extended_tier and (explicit or selected > 900))
    if (selected > 3600 or custom) and not grant:
        raise ExecutionError('job_grant_required', 409)
    if selected > 3600 and not (grant.get('allow_long_runs') and grant.get('allow_extended_jobs')):
        raise ExecutionError('extended_job_grant_required', 409)
    if custom and not grant.get('allow_custom_job_limit'):
        raise ExecutionError('custom_job_grant_required', 409)
    if continuation and grant and not grant['auto_escalate']:
        raise ExecutionError('job_escalation_disabled', 409)
    if grant and grant.get('job_seconds_ceiling') is not None and selected > grant['job_seconds_ceiling']:
        raise ExecutionError('owner_job_ceiling', 409)


def effective_limit(selected, *, run_wall=None, time_budget=None, owner_ceiling=None, installed=3600):
    # Unsupported selections are rejected, rather than quietly shortened.
    if selected > installed:
        raise ExecutionError('installed_job_limit', 409)
    bounds = [('selected_limit', selected), ('run_wall', run_wall),
              ('job_time_budget', time_budget), ('owner_job_ceiling', owner_ceiling)]
    reason, result = min(((k, v) for k, v in bounds if v is not None), key=lambda p: p[1])
    if result < .1:
        raise ExecutionError(reason + '_exhausted', 409)
    return result, reason


def coverage(value):
    """Explicit reviewed v3 scope. No empty digests or path wildcards."""
    only(value, {'profiles', 'workspaces', 'attachments', 'max_job_attempts',
                 'job_seconds_budget', 'allow_cli'})
    profiles = value.get('profiles')
    if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 16:
        raise ExecutionError('invalid_job_coverage')
    for name, revision in profiles.items():
        if name not in {'shell', 'python', 'git', 'mcp', 'codex', 'acceptance', 'proposal', 'checked'} or not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{64}', revision):
            raise ExecutionError('invalid_job_coverage')
    workspaces = value.get('workspaces')
    if not isinstance(workspaces, dict) or not 1 <= len(workspaces) <= 32:
        raise ExecutionError('invalid_job_coverage')
    for workspace, paths in workspaces.items():
        if not isinstance(workspace, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', workspace) or not isinstance(paths, list) or not paths or len(paths) > 32:
            raise ExecutionError('invalid_job_coverage')
        for item in paths:
            if not isinstance(item, str) or not item or item.startswith('/') or '\\' in item or any(p in {'..', ''} for p in item.split('/')):
                raise ExecutionError('invalid_job_coverage')
    attachments = value.get('attachments', [])
    if not isinstance(attachments, list) or len(attachments) > 32 or any(not isinstance(x, str) or not x or len(x) > 128 for x in attachments):
        raise ExecutionError('invalid_job_coverage')
    integer(value.get('max_job_attempts'))
    if value.get('job_seconds_budget') is not None:
        integer(value['job_seconds_budget'])
    if type(value.get('allow_cli')) is not bool:
        raise ExecutionError('invalid_job_coverage')
    return value


def within_coverage(approved, requested):
    """Project v3 grants cover only explicit subsets of the reviewed scope."""
    if not approved or not requested:
        return False
    if requested['max_job_attempts'] > approved['max_job_attempts']:
        return False
    if approved.get('job_seconds_budget') is not None and (requested.get('job_seconds_budget') is None or requested['job_seconds_budget'] > approved['job_seconds_budget']):
        return False
    if requested['allow_cli'] and not approved['allow_cli']:
        return False
    if not set(requested.get('attachments', [])) <= set(approved.get('attachments', [])):
        return False
    if any(approved['profiles'].get(k) != v for k, v in requested['profiles'].items()):
        return False
    for workspace, paths in requested['workspaces'].items():
        roots = approved['workspaces'].get(workspace, [])
        if any(not any(parent == '.' or p == parent or p.startswith(parent + '/') for parent in roots) for p in paths):
            return False
    return True


def permits_job(scope, request, revision):
    if not scope or scope['profiles'].get(request['profile']) != revision:
        return False
    if request['profile'] == 'codex':
        # 4.3f positive acceptance remains a separate gate.
        return False
    cwd = request['cwd'] or '.'
    return any(p == '.' or cwd == p or cwd.startswith(p + '/') for p in scope['workspaces'].get(request['workspace'], []))


@dataclass(frozen=True)
class Reconciliation:
    """Produced by a trusted adapter after hashing the *current* workspace.

    An idle/timeout worker receipt is deliberately insufficient. Adapters must
    establish effects and pass their current digest again at continuation.
    """
    workspace_digest: str
    effects_digest: str
    effects_established: bool
    resumable: bool
    workspace_manifest: object = None
    effects_evidence: object = None

    def established(self, current_digest):
        return ((self.workspace_manifest is None or digest(self.workspace_manifest) == self.workspace_digest)
                and (self.effects_evidence is None or digest(self.effects_evidence) == self.effects_digest)
                and self.effects_established is True and self.resumable is True
                and self.workspace_digest == current_digest
                and all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v)
                        for v in (self.workspace_digest, self.effects_digest)))
