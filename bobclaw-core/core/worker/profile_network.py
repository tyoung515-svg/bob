"""Fixed, approval-free egress for installed Codex/MCP jobs.

Leases start at dispatch, after queue wait, and live only until transport cleanup.
Neither Builder project extras nor model-selected URLs can extend these policies.
"""
import base64

from core.builder.network import DEFAULTS, domain
from core.builder.network_client import JobNetwork
from .contract import WorkerUnavailable


PROFILES = frozenset({'codex', 'claude', 'mcp'})


class ProfileNetwork(JobNetwork):
    def __init__(self, request, config):
        self.profile = request['profile']
        installed = config.get('profiles', {}).get(self.profile, {})
        if (self.profile not in PROFILES or installed.get('network') is not True
                or installed.get('egress') != 'lane-proxy/1'):
            raise WorkerUnavailable('unavailable: installed profile proxy boundary missing')
        # The current MCP job contract (mcp_runner.main) enrolls only local
        # filesystem/browser stdio servers. It contains no configured remote MCP
        # URLs, so no hosts are authorized. In particular, browser navigation
        # arguments and arbitrary extra input keys do not grant network authority.
        self.hosts = frozenset(DEFAULTS['profiles'][self.profile]) if self.profile in {'codex', 'claude'} else frozenset()
        super().__init__(None, None, {}, '', request['execution_id'], config, request['timeout'])

    async def policy(self, value):
        try:
            allowed = domain(value['domain']) in self.hosts and value['method'] == 'CONNECT'
        except (ValueError, KeyError, TypeError):
            allowed = False
        # API calls and refreshes use TLS; CONNECT carries their POST bodies.
        # Plain HTTP never receives provider credentials or upload permission.
        return {'decision': 'allow' if allowed else 'deny'}

    async def log(self, value):
        # The proxy journals each request; serve() retains it in events. The
        # scheduler persists those events with the lane/owner-bound job receipt.
        value['profile'] = self.profile

    def receipt(self, result):
        result = dict(result, network_log=self.events, network_profile=self.profile)
        for stream in ('stdout_b64', 'stderr_b64'):
            if stream in result:
                raw = base64.b64decode(result[stream])
                result[stream] = base64.b64encode(raw.replace(self.token.encode(), b'[job-token]')).decode()
        return result
