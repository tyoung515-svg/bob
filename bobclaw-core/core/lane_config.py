"""Lane authority shared by core and runtime provisioning. No probes or dotenv.

Lane files and that lane's root-published vault enroll providers. The process
environment selects the root (BOB_LANE_ROOT, otherwise the working directory);
it does not supply credentials. Core caches its catalog; enrollment that changes
core.env restarts the lane (the gateway follows core's systemd dependency).
Public readers refresh sanitized metadata; vault rotations need no restart.
"""
from __future__ import annotations

from core.worker import mode as execution_mode
from core.worker import client as worker_client

from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import tomllib

from core.backends.openai_codex_models import enrolled_models


class LaneConfigurationError(ValueError):
    pass


def configured_mode() -> bool:
    return "BOB_ROSTER_FILE" not in os.environ


def normalize_backend(value: str) -> str:
    return "claude_api" if value == "claude" else value


def read_env(path: Path) -> dict[str, str]:
    """The runtime's EnvironmentFile subset, without expansion or execution."""
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise LaneConfigurationError("invalid lane environment file")
        tokens = shlex.split(value, comments=False, posix=True)
        if len(tokens) > 1:
            raise LaneConfigurationError("unquoted lane environment value")
        result[key] = tokens[0] if tokens else ""
    return result


def _setup_data() -> dict:
    path = Path(__file__).resolve().parents[2] / "deploy/runtime/provider-setup.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise LaneConfigurationError("unsupported provider setup schema")
    return data


def provider_setup() -> dict:
    return _setup_data()["providers"]


def transport_defaults() -> dict[str, str]:
    return _setup_data()["transports"]


# Implemented key transports: credentials alone enroll only the setup binding.
# Additional tiers require an explicit BOB_LANE_BACKENDS entry AND a model.
BACKEND_FIELDS = {
    "claude_api": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
    "deepseek_v4_flash": ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"),
    "deepseek_v4": ("DEEPSEEK_API_KEY", "DEEPSEEK_PRO_MODEL"),
    "gemini_flash": ("GOOGLE_API_KEY", "GEMINI_FLASH_MODEL"),
    "gemini_pro": ("GOOGLE_API_KEY", "GEMINI_PRO_MODEL"),
    "gemini_deep_research": ("GOOGLE_API_KEY", "GEMINI_DEEP_RESEARCH_MODEL"),
    "minimax": ("MINIMAX_API_KEY", "MINIMAX_MODEL"),
    "glm_5_3": ("ZAI_API_KEY", "ZAI_MODEL"),
    "glm_5_3_flash_or": ("OPENROUTER_API_KEY", "OPENROUTER_GLM_FLASH_MODEL"),
    "kimi_code": ("KIMI_API_KEY", "KIMI_MODEL"),
    "qwen_cloud": ("DASHSCOPE_API_KEY", "QWEN_MODEL"),
    "kimi_platform": ("MOONSHOT_API_KEY", "KIMI_PLATFORM_MODEL"),
}
CLI_FIELDS = {
    "codex_code": ("CODEX_HOME", "codex-home", "auth.json", "CODEX_CLI_PATH", "codex"),
    "claude_code": ("CC_CONFIG_DIR", "cli/claude", ".credentials.json", "CC_CLI_PATH", "claude"),
    "agy_code": ("AGY_HOME", "cli/agy", ".gemini/antigravity-cli/settings.json", "AGY_CLI_PATH", "agy"),
}


@dataclass(frozen=True)
class LaneBackend:
    backend: str
    kind: str
    model: str | None = None
    available: bool = True
    reason: str | None = None
    models: tuple[str, ...] = ()

    def row(self) -> dict:
        value = dict(backend=self.backend, kind=self.kind, model=self.model,
                     models=list(self.models), available=self.available)
        if self.reason:
            value["unavailable_reason"] = self.reason
        if self.backend == "openai_codex":
            value.update(label=("GPT (ChatGPT sign-in) / " + self.model if self.model else
                                "GPT (ChatGPT sign-in): choose a model"), tool_capable=True)
            if self.model:
                value.update(model_identity="openai/" + self.model, family="openai")
            else:
                value["model_selection_required"] = True
        if self.backend == 'claude_code':
            value.update(label='Claude subscription / ' + str(self.model),
                         tool_capable=execution_mode() == 'worker', billing='subscription',
                         model_identity='anthropic/claude', family='anthropic')
        if self.backend == "codex_code":
            value.update(label="Codex coding agent" + (" / " + self.model if self.model else " (CLI default)"),
                         tool_capable=False)
        from core.model_choices import with_choices
        return with_choices(value)


@dataclass
class LaneConfiguration:
    root: Path
    values: dict[str, str] = field(repr=False)
    backends: dict[str, LaneBackend]
    default_backend: str | None

    def owned_path(self, value: str) -> Path | None:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        return candidate if candidate.is_relative_to(self.root) else None

    def default_error(self) -> str | None:
        if not self.default_backend:
            return "Setup required: select a configured lane default backend"
        try:
            self.require(self.default_backend)
        except LaneConfigurationError as exc:
            return f"Lane default setup error: {exc}"
        return None

    def require(self, backend: str, model: str | None = None) -> LaneBackend:
        backend = normalize_backend(backend)
        if backend in {"ollama", "lmstudio"}:
            backend = "local"
        row = self.backends.get(backend)
        if row is None:
            raise LaneConfigurationError(f"needs provider {backend}: not configured in this lane")
        if execution_mode() == "worker":
            if backend == 'codex_code' and (row.available or (row.reason or '').startswith('unavailable:')):
                reason = unavailable_reason_for_lane(self)
                if reason:
                    raise LaneConfigurationError(reason)
                row = LaneBackend(row.backend, row.kind, row.model, True, None, row.models)
        if execution_mode() == 'worker' and backend == 'claude_code':
            reason = claude_unavailable_reason(self)
            row = LaneBackend(row.backend, row.kind, row.model, not reason, reason, row.models)
        if not row.available:
            raise LaneConfigurationError(f"needs provider {backend}: {row.reason}")
        if model and model not in row.models:
            from core.model_choices import provider_label
            raise LaneConfigurationError(f'The model "{model}" is not offered by {provider_label(row.row())}.')
        return row


def codex_auth_path(lane: LaneConfiguration) -> Path:
    """The lane's credential file, including symlink confinement; never host/env auth."""
    home_key, home_default, auth_name, _, _ = CLI_FIELDS["codex_code"]
    if execution_mode() == "worker":
        if lane.values.get('BOB_WORKER_ID'):
            home = Path(lane.values.get(home_key) or home_default)
            if not home.is_absolute():
                home = lane.root / home
            if '..' in home.parts or not home.is_relative_to(lane.root):
                raise LaneConfigurationError('Codex auth must belong to this lane')
            return home / auth_name
    home = lane.owned_path(lane.values.get(home_key) or home_default)
    path = lane.owned_path(str(home / auth_name)) if home else None
    if path is None:
        raise LaneConfigurationError("Codex auth must belong to this lane")
    return path


def read_codex_auth(lane: LaneConfiguration) -> dict:
    try:
        if execution_mode() == "worker":
            if lane.values.get('BOB_WORKER_ID'):
                from core.worker.auth_file import read
                return read(codex_auth_path(lane), int(lane.values['BOB_WORKER_UID']))
        auth = json.loads(codex_auth_path(lane).read_text(encoding="utf-8"))
        return auth if isinstance(auth, dict) else {}
    except (OSError, ValueError):
        return {}


def codex_signin(auth: dict) -> bool:
    tokens = auth.get("tokens")
    return isinstance(tokens, dict) and any(
        isinstance(tokens.get(k), str) and bool(tokens[k]) for k in ("access_token", "refresh_token"))


def read_lane(root: Path, *, values: dict | None = None) -> LaneConfiguration:
    root = root.resolve()
    # Match systemd order. Neither shared secrets nor the host env are sources.
    if any(not (root / name).resolve().is_relative_to(root) for name in ("lane.env", "providers.env")):
        raise LaneConfigurationError("lane source must belong to this lane")
    public = False
    if values is not None:
        values = dict(values)  # Root provisioning's already-parsed, private input.
    elif os.environ.get('BOB_SETTINGS_ID'):
        source = 'core.env' if os.environ.get('BOB_SERVICE_ROLE') == 'core' else 'public.env'
        values = read_env(root / source)
        if source == 'core.env':
            values.update(read_env(root / 'providers.env'))
        else:
            public = True
    else:
        values = {**read_env(root / "lane.env"), **read_env(root / "providers.env")}
    # A vault entry supplies only its preset's enrollment credential. It must
    # not activate another tier that happens to share an API-key variable.
    prior_keys = {key: bool(values.get(key)) for key, _ in BACKEND_FIELDS.values()}
    vault_presets = {spec['backend'] for spec in provider_setup().values()}
    vault_id = values.get('BOB_SETTINGS_ID') or values.get('BOB_WORKER_ID')
    if not public and vault_id:
        from core.settings_client import vault_key
        from core.settings_contract import PROVIDERS
        for provider, key in PROVIDERS.items():
            values[key] = vault_key(provider, vault_id, values.get(key, ''))
    default = normalize_backend(values.get("BOB_LANE_DEFAULT_BACKEND", "").strip()) or None
    lane = LaneConfiguration(root, values, {}, default)
    if public:
        # Gateway/bridge need catalog metadata, never provider or CLI credentials.
        for row in json.loads(values.get('BOB_PUBLIC_BACKENDS', '[]')):
            lane.backends[row['backend']] = LaneBackend(row['backend'], row['kind'], row['model'],
                row['available'], row.get('unavailable_reason'), tuple(row['models']))
        return lane
    explicit = {normalize_backend(b.strip()) for b in values.get("BOB_LANE_BACKENDS", "").split(",") if b.strip()}
    enrolled = explicit | {s["backend"] for s in provider_setup().values()}
    disabled = {normalize_backend(b.strip()) for b in values.get("BOB_UNAVAILABLE_BACKENDS", "").split(",")}
    for backend, (key, model_key) in BACKEND_FIELDS.items():
        if backend not in enrolled or not values.get(key):
            continue
        if backend not in vault_presets and not prior_keys[key]:
            continue
        model = values.get(model_key) or None
        reason = "entitlement_lapsed" if backend in disabled else (None if model else "model_configuration_required")
        lane.backends[backend] = LaneBackend(backend, "cloud", model, not reason, reason,
                                              (model,) if model else ())
    for backend, (home_key, home_default, auth_name, binary_key, binary) in CLI_FIELDS.items():
        # Claude enrollment is root-published sanitized status, without a core
        # read grant to the subscription credential. Turn readiness is separate.
        if execution_mode() == 'worker' and backend == 'claude_code':
            from core.worker.claude import MODELS
            model = values.get('CLAUDE_CODE_MODEL')
            if values.get('CLAUDE_SIGNED_IN') == 'true' and model in MODELS:
                reason = claude_unavailable_reason(lane)
                lane.backends[backend] = LaneBackend(backend, 'cli', model, not reason, reason, tuple(MODELS))
            continue
        if execution_mode() == "worker" and backend != "codex_code":
            continue
        shared = execution_mode() == "worker" and bool(values.get("BOB_WORKER_ID"))
        if backend != "codex_code" and backend not in explicit:
            continue
        home = codex_auth_path(lane).parent if shared else lane.owned_path(values.get(home_key) or home_default)
        if home is None:
            continue
        auth_file = home / auth_name if shared else lane.owned_path(str(home / auth_name))
        if auth_file is None:
            continue
        try:
            auth = read_codex_auth(lane) if backend == "codex_code" else json.loads(auth_file.read_text(encoding="utf-8"))
            if not isinstance(auth, dict):
                continue
            if backend == "codex_code":
                tokens = auth.get("tokens") or {}
                if not (auth.get("OPENAI_API_KEY") or (isinstance(tokens, dict) and
                        (tokens.get("access_token") or tokens.get("refresh_token")))):
                    continue
            elif not auth:
                continue
        except (OSError, ValueError):
            continue
        model = values.get("CODEX_MODEL" if backend == "codex_code" else backend.upper() + "_MODEL") or None
        profile_error = None
        if backend == "codex_code":
            config_path = home / "config.toml" if shared else lane.owned_path(str(home / "config.toml"))
            try:
                if shared:
                    from core.worker.auth_file import configuration_text
                    cli = tomllib.loads(configuration_text(home, int(values["BOB_WORKER_UID"])))
                else:
                    cli = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path else {}
                profile = values.get("CODEX_PROFILE", "gpt")
                selected = cli.get("profiles", {}).get(profile, {})
                # A profile for another provider never enrolls that provider.
                if selected.get("model_provider", cli.get("model_provider", "openai")) == "openai":
                    model = model or selected.get("model") or cli.get("model") or None
                else:
                    profile_error = "profile_provider_not_configured"
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError, TypeError):
                profile_error = "invalid_cli_configuration"
        if model is not None and not isinstance(model, str):
            model, profile_error = None, "invalid_model_configuration"
        reason = "entitlement_lapsed" if backend in disabled else profile_error or (
            None if shutil.which(values.get(binary_key) or binary) else "executable_missing")
        lane.backends[backend] = LaneBackend(backend, "cli", model, not reason, reason,
                                              (model,) if model else ())
        if backend == "codex_code" and codex_signin(auth):
            http_model = values.get("OPENAI_CODEX_MODEL") or model
            # The coding agent follows the owner's selected ChatGPT model too.
            # A CLI default with no identity is not a catalog binding.
            if http_model:
                lane.backends[backend] = LaneBackend(backend, 'cli', http_model,
                    not reason, reason, (http_model,))
            # HTTP does not execute the CLI profile; broken/missing executables
            # or CLI-only provider settings do not disable ChatGPT transport.
            http_reason = "entitlement_lapsed" if "openai_codex" in disabled else None
            try:
                visible = json.loads(values.get("OPENAI_CODEX_MODELS", "[]"))
                if not isinstance(visible, list):
                    visible = []
            except ValueError:
                visible = []
            lane.backends["openai_codex"] = LaneBackend(
                "openai_codex", "cloud", http_model, not http_reason, http_reason,
                enrolled_models(visible, http_model))
    if execution_mode() == "worker":
        from core.worker.client import EXCLUDED, EXCLUDED_REASON
        for backend in sorted(explicit & EXCLUDED):
            lane.backends[backend] = LaneBackend(backend, 'cli', available=False, reason=EXCLUDED_REASON)
        if 'codex_code' in lane.backends:
            reason = unavailable_reason_for_lane(lane)
            if reason:
                row = lane.backends['codex_code']
                lane.backends['codex_code'] = LaneBackend(row.backend, row.kind, row.model, False, reason, row.models)
    return lane


def claude_observations(lane):
    from core.backends.claude_state import load
    return load(lane.root, lane.values.get('CLAUDE_AUTH_GENERATION', ''))


def claude_unavailable_reason(lane):
    if 'claude_code' in lane.values.get('BOB_UNAVAILABLE_BACKENDS', '').split(','):
        return 'entitlement_lapsed'
    try:
        if claude_observations(lane).get('status') == 'unknown':
            return 'Sign in to Claude again'
        key = lane.values.get('BOB_WORKER_ID')
        if not key:
            return 'unavailable: lane has no Claude worker boundary'
        from core.worker.contract import root_json
        cfg = root_json(Path('/etc/bob/workers') / (key + '.json'))
        if (cfg.get('profiles', {}).get('claude', {}).get('argv') != ['/usr/local/bin/claude']
                or not Path(cfg.get('claude_turn_socket', '/nonexistent')).is_socket()):
            return 'unavailable: Claude turn service missing; run CC2 provisioning'
        return unavailable_reason_for_lane(lane)
    except (OSError, ValueError, RuntimeError):
        return 'unavailable: Claude turn service or status missing'


def unavailable_reason_for_lane(lane):
    # No get_lane recursion while constructing the configured catalog.
    if not lane.values.get('BOB_WORKER_ID'):
        return 'unavailable: lane has no worker boundary; migration required'
    from core.worker.contract import root_json, WorkerUnavailable
    try:
        lane_id = lane.values['BOB_WORKER_ID']
        cfg = root_json(Path('/etc/bob/workers') / (lane_id + '.json'))
        marker = root_json(Path('/run/bob-workers') / lane_id / 'policy.json')
        if marker.get('worker_uid') != cfg['worker_uid'] or not Path(cfg['socket']).is_socket():
            return 'unavailable: worker service or network policy missing'
    except (OSError, ValueError, WorkerUnavailable):
        return 'unavailable: worker service or network policy missing'
    return None


@lru_cache(maxsize=None)
def _cached_lane(root: Path) -> LaneConfiguration:
    return read_lane(root)


def get_lane() -> LaneConfiguration:
    root = Path(os.environ.get("BOB_LANE_ROOT") or Path.cwd()).resolve()
    # Public readers see regenerated metadata even before a deferred lane restart.
    if os.environ.get('BOB_SETTINGS_ID') and os.environ.get('BOB_SERVICE_ROLE') != 'core':
        return read_lane(root)
    return _cached_lane(root)


def require_backend(backend: str, model: str | None = None) -> None:
    if execution_mode() == "worker":
        from core.worker.client import EXCLUDED, EXCLUDED_REASON
        # The confined Claude turn path exists only for a configured lane.
        if backend in EXCLUDED or (backend == 'claude_code' and not configured_mode()):
            raise LaneConfigurationError(f'needs provider {backend}: {EXCLUDED_REASON}')
    if configured_mode():
        get_lane().require(backend, model)


def require_state(state: dict) -> None:
    """Validate a turn/worker's declared providers before its first model call."""
    if not configured_mode():
        return
    from core import teams
    teams.require_team(state.get("team") or teams.get_active_team())
    model = (state.get("model_override") or (state.get("codex_posture") or {}).get("model")
             or (state.get("agy_posture") or {}).get("model") or (state.get("pi_posture") or {}).get("model"))
    require_backend(state.get("backend"), model)
    for key in ("escalation_backend", "critic_backend", "apex_backend"):
        if state.get(key):
            require_backend(state[key])
    for backend in state.get("critic_fallback_chain") or []:
        require_backend(backend)
    if state.get("backend") == "codex_code":
        requested = (state.get("codex_posture") or {}).get("profile")
        selected = get_lane().values.get("CODEX_PROFILE", "gpt")
        if requested and requested != selected:
            raise LaneConfigurationError(f"needs provider codex_code profile configuration {requested}")
        if (state.get("codex_posture") or {}).get("extra_args"):
            raise LaneConfigurationError("needs provider/configuration: CLI argument overrides are not lane presets")


def selectable_backends() -> set[str]:
    if not configured_mode():
        from core.config import KNOWN_BACKENDS
        result = set(KNOWN_BACKENDS)
        if "openai_codex" not in get_lane().backends:
            result.discard("openai_codex")
        return result
    return {name for name, row in get_lane().backends.items()
            if name not in {'codex_code', 'claude_code'} or row.model}


async def catalog(router) -> list[dict]:
    """Sanitized effective rows; local membership requires a permitted chat probe."""
    lane = get_lane()
    if execution_mode() == "worker":
        row = lane.backends.get('codex_code')
        if row and (row.available or (row.reason or '').startswith('unavailable:')):
            reason = unavailable_reason_for_lane(lane)
            lane.backends['codex_code'] = LaneBackend(row.backend, row.kind, row.model,
                                                     not reason, reason, row.models)
    if any(lane.values.get(k) for k in ("OLLAMA_URL", "LMSTUDIO_URL")):
        try:
            found = await router.discover()
        except Exception:
            found = []
        permitted = {lane.values.get(k, "").rstrip("/") for k in ("OLLAMA_URL", "LMSTUDIO_URL")}
        models = sorted({m for b in found if b.url.rstrip("/") in permitted
                         for m in b.models if chat_model(m)})
        if models:
            disabled = "local" in {b.strip() for b in lane.values.get("BOB_UNAVAILABLE_BACKENDS", "").split(",")}
            lane.backends["local"] = LaneBackend("local", "local", available=not disabled,
                                                reason="entitlement_lapsed" if disabled else None,
                                                models=tuple(models))
        else:
            lane.backends.pop("local", None)
    if execution_mode() == 'worker' and 'claude_code' in lane.backends:
        row = lane.backends['claude_code']
        reason = claude_unavailable_reason(lane)
        lane.backends['claude_code'] = LaneBackend(row.backend, row.kind, row.model, not reason, reason, row.models)
    rows = [lane.backends[b].row() for b in sorted(lane.backends)
            if b not in {'codex_code', 'claude_code'} or lane.backends[b].model]
    for row in rows:
        if row['backend'] == 'claude_code' and execution_mode() == 'worker':
            try:
                observed = claude_observations(lane)
                row['provider_reported_model'] = observed.get('models', {}).get(row['model'])
                row['subscription_status'] = observed.get('status', 'signed_in')
                from core.model_choices import with_choices
                row.update(with_choices(row, observations=observed.get('models', {})))
            except (OSError, ValueError):
                row['subscription_status'] = 'unknown'
    return rows


def agent_service_definitions(rows: list[dict]) -> dict:
    """Text aliases for concrete catalog bindings, never an inferred CLI pin.

    Include the backend in the identity so two transports for the same model
    remain distinct. The digest keeps model paths/spaces safe in provider IDs.
    Availability is configuration evidence, not a claim of inference health.
    """
    models = {}
    presets = provider_setup().values()
    for row in rows:
        # Expanding chat aliases must not expand workflow/stage bindings this round.
        binding_models = ([row['model']] if row['backend'] == 'claude_code' else row['models'])
        for model in sorted(set(binding_models)):
            alias = f"bob-{row['backend']}-{hashlib.sha256(model.encode()).hexdigest()[:16]}"
            label = ("GPT (ChatGPT sign-in)" if row['backend'] == 'openai_codex' else
                     "Codex coding agent" if row['backend'] == 'codex_code' else
                     "Claude subscription" if row['backend'] == 'claude_code' else row['backend'])
            spec = {"label": f"{label} / {model}", "backend": row["backend"],
                    "model": model, "transport": "text", "enabled": row["available"]}
            if row['backend'] == 'openai_codex':
                spec.update(model_identity='openai/' + model, family='openai')
            if row['backend'] == 'claude_code' and execution_mode() == 'worker':
                # Alias selections intentionally have no declared full-name list:
                # BU5 records the first name and locks it for the run.
                spec.update(model_identity='anthropic/claude', family='anthropic')
            for preset in presets:
                if preset['backend'] == row['backend'] and preset['model'] == model:
                    for key in ('model_identity', 'family'):
                        if preset.get(key):
                            spec[key] = preset[key]
                if (preset['backend'] == row['backend'] and preset['model'] == model
                        and preset.get('reported_model_names')):
                    spec['reported_model_names'] = list(preset['reported_model_names'])
            if row.get("unavailable_reason"):
                spec["disabled_reason"] = row["unavailable_reason"]
            models[alias] = spec
    return {"models": models, "teams": {}, "worker_choices": {}}


def chat_model(model: str) -> bool:
    name = model.lower().rsplit("/", 1)[-1]
    return bool(model) and not any(x in name for x in ("embed", "rerank")) and not name.startswith(("bge-", "e5-", "gte-"))


def chat_model_entry(entry: dict) -> bool:
    metadata = " ".join(str(entry.get(key, "")) for key in ("type", "task", "capabilities", "output_modalities"))
    return chat_model(entry.get("id", "")) and not any(x in metadata.lower() for x in ("embed", "rerank"))


def require_council(spec: dict) -> None:
    """All seats and auxiliaries are admitted together, before the first call."""
    if not configured_mode():
        return
    profile = spec.get("profile") or {}
    if not spec.get("seats") or not spec.get("synth_backend"):
        raise LaneConfigurationError("needs provider/configuration council seats and synthesizer")
    for posture in spec["seats"]:
        seat = profile.get(posture) or {}
        require_backend(seat.get("backend"))
        for backend in seat.get("fallback_chain") or []:
            require_backend(backend)
    require_backend(spec["synth_backend"])
    for backend in spec.get("synth_fallback_chain") or []:
        require_backend(backend)
    from core.nodes.grounding import grounding_enabled
    if grounding_enabled(spec):
        require_backend(get_lane().values.get("COUNCIL_GROUND_BACKEND"))
