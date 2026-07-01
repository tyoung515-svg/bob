"""
Tests for BoBClaw Gateway configuration validation (B5).

Covers:
 - Rejection of empty secrets (JWT_SECRET, BOBCLAW_PASSWORD, TOTP_SECRET)
 - Minimum length enforcement on JWT_SECRET (>= 32)
 - Blocklist rejection for trivial/placeholder secrets
 - Unsafe startup bypass gating (BOBCLAW_ALLOW_UNSAFE)
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure gateway package is importable
_GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_DIR))

from config import BoBClawGatewayConfig


class TestConfigValidation:
    """Unit tests for config.validate() behaviour."""

    def test_validate_raises_on_empty_jwt_secret(self, monkeypatch):
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", "")
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", "validpassword123")
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", "valid-totp-secret")
        with pytest.raises(ValueError, match="JWT_SECRET"):
            BoBClawGatewayConfig.validate()

    def test_validate_raises_on_empty_password(self, monkeypatch):
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", "a" * 32)
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", "")
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", "valid-totp-secret")
        with pytest.raises(ValueError, match="BOBCLAW_PASSWORD"):
            BoBClawGatewayConfig.validate()

    def test_validate_raises_on_empty_totp_secret(self, monkeypatch):
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", "a" * 32)
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", "validpassword123")
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", "")
        with pytest.raises(ValueError, match="TOTP_SECRET"):
            BoBClawGatewayConfig.validate()

    def test_validate_raises_on_short_jwt_secret(self, monkeypatch):
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", "short")
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", "validpassword123")
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", "valid-totp-secret")
        with pytest.raises(ValueError, match="at least 32"):
            BoBClawGatewayConfig.validate()

    @pytest.mark.parametrize(
        "bad_secret",
        ["changeme", "secret", "password", "bobclaw", "None"],
    )
    def test_validate_raises_on_placeholder_jwt_secret(self, monkeypatch, bad_secret):
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", bad_secret)
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", "validpassword123")
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", "valid-totp-secret")
        with pytest.raises(ValueError, match="placeholder"):
            BoBClawGatewayConfig.validate()

    @pytest.mark.parametrize(
        "bad_password",
        ["changeme", "secret", "password", "bobclaw", "None"],
    )
    def test_validate_raises_on_placeholder_password(self, monkeypatch, bad_password):
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", "a" * 32)
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", bad_password)
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", "valid-totp-secret")
        with pytest.raises(ValueError, match="placeholder"):
            BoBClawGatewayConfig.validate()

    @pytest.mark.parametrize(
        "bad_totp",
        ["changeme", "secret", "password", "bobclaw", "None"],
    )
    def test_validate_raises_on_placeholder_totp_secret(self, monkeypatch, bad_totp):
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", "a" * 32)
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", "validpassword123")
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", bad_totp)
        with pytest.raises(ValueError, match="placeholder"):
            BoBClawGatewayConfig.validate()

    def test_validate_passes_with_strong_secrets(self, monkeypatch):
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", "a" * 32)
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", "validpassword123")
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", "valid-totp-secret")
        # Should not raise
        BoBClawGatewayConfig.validate()


class TestUnsafeStartupBypass:
    """Integration tests for the --skip-validation gating in main()."""

    def test_main_exits_on_missing_secrets(self, monkeypatch):
        """When secrets are missing and BOBCLAW_ALLOW_UNSAFE is unset, main() exits."""
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", "")
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", "")
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", "")

        from gateway import main

        with patch.object(sys, "argv", ["gateway.py"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2

    def test_main_accepts_skip_validation_with_env_flag(self, monkeypatch):
        """--skip-validation is accepted when BOBCLAW_ALLOW_UNSAFE=1."""
        monkeypatch.setenv("BOBCLAW_ALLOW_UNSAFE", "1")
        monkeypatch.setattr(BoBClawGatewayConfig, "JWT_SECRET", "")
        monkeypatch.setattr(BoBClawGatewayConfig, "BOBCLAW_PASSWORD", "")
        monkeypatch.setattr(BoBClawGatewayConfig, "TOTP_SECRET", "")

        from gateway import main

        # Patch sys.argv to include --skip-validation and prevent web.run_app blocking.
        with patch.object(sys, "argv", ["gateway.py", "--skip-validation"]):
            with patch("gateway.web.run_app") as mock_run:
                with patch("gateway.sys.exit") as mock_exit:
                    main()
        # Should NOT call sys.exit(2) for validation failure
        validation_exit_calls = [
            c for c in mock_exit.call_args_list if c.args and c.args[0] == 2
        ]
        assert not validation_exit_calls
        # web.run_app should have been called (server starts)
        assert mock_run.called
