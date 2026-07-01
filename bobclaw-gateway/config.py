"""
BoBClaw Gateway — Configuration
Loads from ../.secrets/bobclaw.env or local .env
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load env: check .secrets first, then local
_SECURE_ENV = Path(__file__).parent.parent / ".secrets" / "bobclaw.env"
_LOCAL_ENV = Path(__file__).parent / ".env"

# override=False: real process env (launchers / pytest conftest setdefault) wins
# over .secrets — prevents .secrets leaking into pytest. (Matches pipeline config.)
if _SECURE_ENV.exists():
    load_dotenv(_SECURE_ENV, override=False)
else:
    load_dotenv(_LOCAL_ENV)


# Secrets that are trivially guessable and must be rejected.
_SECRET_BLOCKLIST: frozenset[str] = frozenset(
    {"changeme", "secret", "password", "bobclaw", "none", ""}
)


class BoBClawGatewayConfig:
    """Gateway service configuration."""

    # -- Server --
    PORT: int = int(os.getenv("BOBCLAW_GATEWAY_PORT", "7826"))
    # Loopback by default. The gateway is the only service meant to face clients,
    # but it should still be reached over 127.0.0.1 (or a reverse proxy / SSH
    # tunnel you place in front of it), never bound to 0.0.0.0. See SECURITY.md.
    HOST: str = os.getenv("BOBCLAW_GATEWAY_HOST", "127.0.0.1")

    # -- TLS --
    TLS_ENABLED: bool = os.getenv("TLS_ENABLED", "false").lower() == "true"
    TLS_CERT: str = os.getenv("TLS_CERT", "ssl/cert.pem")
    TLS_KEY: str = os.getenv("TLS_KEY", "ssl/key.pem")

    # -- Auth --
    # Support both JWT_SECRET (gateway-specific) and BOBCLAW_SECRET (shared secret)
    JWT_SECRET: str = os.getenv("JWT_SECRET") or os.getenv("BOBCLAW_SECRET", "")
    # The SHARED secret core also reads (core.config.BOBCLAW_SECRET) — read explicitly
    # (not via the JWT_SECRET fallback) so the gateway→core scope vouch (Neck Beard P3)
    # is keyed on the same value on both sides even when a gateway-only JWT_SECRET is
    # set. Empty ⇒ the vouch is "" ⇒ core fails closed (scope not honored).
    BOBCLAW_SECRET: str = os.getenv("BOBCLAW_SECRET", "")
    BOBCLAW_PASSWORD: str = os.getenv("BOBCLAW_PASSWORD", "")
    TOTP_SECRET: str = os.getenv("TOTP_SECRET", "")

    # -- Token lifetimes --
    ACCESS_TOKEN_MINUTES: int = int(os.getenv("ACCESS_TOKEN_MINUTES", "15"))
    REFRESH_TOKEN_DAYS: int = int(os.getenv("REFRESH_TOKEN_DAYS", "90"))
    # Scoped agent (Neck Beard MODE) bearer tokens — standalone, non-refreshable
    # in v0. Longer-lived than the 15-min human access token because a headless
    # agent has no interactive refresh loop yet (Phase 5 adds revocation/refresh).
    AGENT_TOKEN_DAYS: int = int(os.getenv("AGENT_TOKEN_DAYS", "30"))

    # -- Rate limiting (in-memory; per-process) --
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    RATE_LIMIT_BURST: int = int(os.getenv("RATE_LIMIT_BURST", "60"))

    # -- CORS --
    ALLOWED_ORIGINS: list[str] = [
        origin.strip()
        for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]

    # -- Upstream services --
    CORE_URL: str = os.getenv("CORE_URL", "http://localhost:7825")
    CLAUDE_PIPELINE_URL: str = os.getenv("CLAUDE_PIPELINE_URL", "http://localhost:7823")
    CANOPY_URL: str = os.getenv("CANOPY_URL", "http://localhost:7822")

    # -- Database --
    POSTGRES_URL: str = os.getenv(
        "POSTGRES_URL", "postgresql://bobclaw:bobclaw@localhost:5432/bobclaw"
    )

    # -- Redis (pub/sub for live approval notifications) --
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # -- Logging --
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # -- History replay (trailing-N context for conversation continuity) --
    HISTORY_MESSAGE_COUNT: int = int(os.getenv("BOBCLAW_HISTORY_MESSAGES", "20"))
    HISTORY_MAX_CHARS: int = int(os.getenv("BOBCLAW_HISTORY_MAX_CHARS", "8192"))

    # -- Audit logging --
    AUDIT_LOG_ENABLED: bool = os.getenv("AUDIT_LOG_ENABLED", "true").lower() == "true"

    @classmethod
    def validate(cls) -> None:
        """Validate required configuration. Raises ValueError on missing fields."""
        errors = []

        # JWT_SECRET
        if not cls.JWT_SECRET:
            errors.append("JWT_SECRET (or BOBCLAW_SECRET) is required")
        else:
            if len(cls.JWT_SECRET) < 32:
                errors.append("JWT_SECRET must be at least 32 characters")
            if cls.JWT_SECRET.lower() in _SECRET_BLOCKLIST:
                errors.append(f"JWT_SECRET is too common / placeholder: {cls.JWT_SECRET!r}")

        # BOBCLAW_PASSWORD
        if not cls.BOBCLAW_PASSWORD:
            errors.append("BOBCLAW_PASSWORD is required")
        if cls.BOBCLAW_PASSWORD and cls.BOBCLAW_PASSWORD.lower() in _SECRET_BLOCKLIST:
            errors.append(
                f"BOBCLAW_PASSWORD is too common / placeholder: {cls.BOBCLAW_PASSWORD!r}"
            )

        # TOTP_SECRET
        if not cls.TOTP_SECRET:
            errors.append("TOTP_SECRET is required")
        if cls.TOTP_SECRET and len(cls.TOTP_SECRET) < 16:
            errors.append("TOTP_SECRET must be at least 16 characters")
        if cls.TOTP_SECRET and cls.TOTP_SECRET.lower() in _SECRET_BLOCKLIST:
            errors.append(
                f"TOTP_SECRET is too common / placeholder: {cls.TOTP_SECRET!r}"
            )

        if errors:
            raise ValueError(f"Config errors: {'; '.join(errors)}")


config = BoBClawGatewayConfig()
