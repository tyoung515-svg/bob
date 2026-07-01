from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from core.memory.decay import credibility_mean, decay_alpha
from core.memory.models import ConfidenceStub


class TestDecayAlpha:
    def test_stable_biographical_never_decays(self):
        alpha = decay_alpha(10.0, "2025-01-01T00:00:00+00:00", "stable_biographical", datetime(2026, 5, 13, tzinfo=timezone.utc))
        assert alpha == 10.0

    def test_recent_status_halved_at_half_life(self):
        alpha = decay_alpha(
            10.0,
            "2025-05-13T00:00:00+00:00",
            "recent_status",
            datetime(2025, 5, 20, tzinfo=timezone.utc),
        )
        assert abs(alpha - 5.0) < 1e-9

    def test_none_ts_returns_alpha_unchanged(self):
        alpha = decay_alpha(7.5, None, "recent_status", datetime(2026, 5, 13, tzinfo=timezone.utc))
        assert alpha == 7.5

    def test_no_elapsed_time_no_decay(self):
        now = datetime(2025, 5, 13, tzinfo=timezone.utc)
        alpha = decay_alpha(10.0, "2025-05-13T00:00:00+00:00", "recent_status", now)
        assert alpha == 10.0

    def test_future_timestamp_no_decay(self):
        alpha = decay_alpha(
            10.0,
            "2026-06-01T00:00:00+00:00",
            "recent_status",
            datetime(2025, 5, 13, tzinfo=timezone.utc),
        )
        assert alpha == 10.0

    def test_unknown_decay_class_returns_alpha(self):
        alpha = decay_alpha(5.0, "2025-01-01T00:00:00+00:00", "nonexistent", datetime(2026, 5, 13, tzinfo=timezone.utc))
        assert alpha == 5.0

    def test_z_suffix_parsed_correctly(self):
        alpha = decay_alpha(10.0, "2025-05-13T00:00:00Z", "recent_status", datetime(2025, 5, 20, tzinfo=timezone.utc))
        assert abs(alpha - 5.0) < 1e-9


class TestCredibilityMean:
    def test_bounded_0_to_1(self):
        high = credibility_mean(ConfidenceStub(alpha=1e6, beta=1.0, decay_class="stable_biographical"))
        assert 0.0 <= high <= 1.0

        low = credibility_mean(ConfidenceStub(alpha=0.0, beta=1e6, decay_class="stable_biographical"))
        assert 0.0 <= low <= 1.0

    def test_zero_denom_returns_zero(self):
        mean = credibility_mean(ConfidenceStub(alpha=0.0, beta=0.0, decay_class="stable_biographical"))
        assert mean == 0.0

    def test_heavily_decayed_close_to_prior(self):
        one_year_ago = "2025-05-13T00:00:00+00:00"
        confidence = ConfidenceStub(
            alpha=100.0, beta=1.0,
            decay_class="recent_status",
            last_corroboration_ts=one_year_ago,
        )
        now = datetime(2026, 5, 13, tzinfo=timezone.utc)
        mean = credibility_mean(confidence, now)
        assert mean < 0.01

    def test_no_corroboration_ts_uses_stored_alpha(self):
        mean = credibility_mean(
            ConfidenceStub(alpha=4.0, beta=1.0, decay_class="recent_status"),
            datetime(2026, 5, 13, tzinfo=timezone.utc),
        )
        assert abs(mean - 0.8) < 1e-9

    def test_default_now_uses_datetime_now(self):
        mean = credibility_mean(ConfidenceStub(alpha=1.0, beta=1.0, decay_class="stable_biographical"))
        assert abs(mean - 0.5) < 1e-9

    @pytest.mark.parametrize("run", range(100))
    def test_property_stable_output(self, run):
        import random
        seed = run * 42
        rng = random.Random(seed)
        now = datetime(2026, 5, 13, tzinfo=timezone.utc)
        alpha = rng.uniform(0.1, 100.0)
        beta = rng.uniform(0.1, 100.0)
        ts = "2026-01-01T00:00:00+00:00"
        classes = ["stable_biographical", "current_role", "recent_status", "preference", "version_dependent", "event_factual"]
        dc = rng.choice(classes)
        c = ConfidenceStub(alpha=alpha, beta=beta, decay_class=dc, last_corroboration_ts=ts)
        m1 = credibility_mean(c, now)
        m2 = credibility_mean(c, now)
        assert m1 == m2
