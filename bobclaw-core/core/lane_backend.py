"""Opt-in runtime lane default. No network probes or production routing changes."""
import json
import os
import shutil
from pathlib import Path

from core.config import config
from core.lane_config import configured_mode, get_lane, normalize_backend

_KEYS = {
    'claude': 'ANTHROPIC_API_KEY', 'claude_api': 'ANTHROPIC_API_KEY',
    'claude_managed': 'ANTHROPIC_API_KEY',
    'deepseek_v4_flash': 'DEEPSEEK_API_KEY', 'deepseek_v4': 'DEEPSEEK_API_KEY',
    'minimax': 'MINIMAX_API_KEY', 'glm_5_3': 'ZAI_API_KEY',
    'glm_5_3_flash_or': 'OPENROUTER_API_KEY', 'kimi_code': 'KIMI_API_KEY',
    'kimi_platform': 'MOONSHOT_API_KEY',
    'qwen_cloud': 'DASHSCOPE_API_KEY',
    'gemini_flash': 'GOOGLE_API_KEY', 'gemini_pro': 'GOOGLE_API_KEY',
    'gemini_deep_research': 'GOOGLE_API_KEY',
}


def has_credential(backend: str) -> bool:
    if backend == 'openai_codex':
        return backend in get_lane().backends
    if configured_mode():
        return normalize_backend(backend) in get_lane().backends
    key = _KEYS.get(backend)
    if key:
        return bool(getattr(config, key, ''))
    if backend == 'codex_code':
        home = config.CODEX_HOME
        if not home:
            return False
        try:
            auth = json.loads((Path(home) / 'auth.json').read_text())
            tokens = auth.get('tokens') or {}
            return bool(auth.get('OPENAI_API_KEY') or tokens.get('access_token')
                        or tokens.get('refresh_token'))
        except (OSError, ValueError, AttributeError):
            return False
    if backend == 'claude_code':
        home = config.CC_CONFIG_DIR
        return bool(home and (Path(home) / '.credentials.json').is_file())
    if backend == 'agy_code':
        return Path(config.AGY_SETTINGS_PATH).is_file()
    if backend in {'kimi_cli', 'pi_code'}:
        key = 'KIMI_CLI_PATH' if backend == 'kimi_cli' else 'PI_CLI_PATH'
        return bool(shutil.which(getattr(config, key, '') or backend.removesuffix('_code').removesuffix('_cli')))
    # Local/unmanaged CLI backends have no API-key contract. Do not assume
    # absent credentials or override an explicitly configured local backend.
    return True


def preferred_for_lane(preferred: str, escalation: str) -> str:
    if configured_mode():
        get_lane().require(preferred)
        return normalize_backend(preferred)
    default = os.environ.get('BOB_LANE_DEFAULT_BACKEND', '').strip()
    if not default:
        return preferred
    if not has_credential(preferred) and not has_credential(escalation):
        return 'claude_api' if default == 'claude' else default
    return preferred
