import sys
import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from core.ledger.types import EXHAUSTED_TAG
from core.verify.postcondition import PostConditionError, is_decorrelated, family_of, decorrelated_critic_backend
from core.gui.types import Subgoal, FailureType
from core.gui.recovery import RecoveryDirective, RecoveryDecision, RecoveryAction
from core.gui.grounders.holo import HOLO_BACKEND
from core.gui.termination import (
    CriterionSource,
    GuiCriterion,
    criteria_for_subgoals,
    mark_verified,
    mark_exhausted,
    apply_recovery,
    is_run_complete,
    run_decision,
    could_not_verify,
    pending,
    grade_criterion,
    make_grader,
)


def fake_send(reply):
    async def _send(messages, backend):
        return reply
    return _send


def raising_send():
    async def _send(messages, backend):
        raise RuntimeError("boom")
    return _send


def test_criteria_for_subgoals():
    # Normal case
    cs = criteria_for_subgoals([Subgoal("open the menu"), "save the file"])
    assert len(cs) == 2
    for c in cs:
        assert c.verified is False
        assert c.exhausted is False
        assert c.tag == "U"
        assert c.sources == ()
    assert cs[0].key == "open the menu"
    assert cs[0].subgoal == "open the menu"
    assert cs[1].key == "save the file"
    assert cs[1].subgoal == "save the file"

    # Duplicate texts
    cs_dup = criteria_for_subgoals(["dup", "dup"])
    assert len(cs_dup) == 2
    assert cs_dup[0].key != cs_dup[1].key
    # A duplicate text is disambiguated with a deterministic "#<idx>" suffix; the subgoal text
    # is preserved on every criterion.
    assert "#" in cs_dup[1].key
    assert cs_dup[0].subgoal == "dup"
    assert cs_dup[1].subgoal == "dup"


def test_mark_verified():
    cs = criteria_for_subgoals(["a", "b"])
    cs2 = mark_verified(cs, "a", source=CriterionSource.GRADER)
    # "a" criterion in cs2 is verified
    a_crit = next(c for c in cs2 if c.key == "a")
    assert a_crit.verified is True
    assert CriterionSource.GRADER.value in a_crit.sources  # "grader"
    assert a_crit.to_criterion().verified is True
    # "b" still unverified
    b_crit = next(c for c in cs2 if c.key == "b")
    assert b_crit.verified is False
    # Original cs unchanged (frozen)
    assert cs[0].verified is False

    # Unknown key
    cs3 = mark_verified(cs, "missing", source=CriterionSource.GRADER)
    assert cs3 == cs  # same list (should be equal element-wise)
    # Ensure no exception raised


def test_mark_exhausted():
    cs = criteria_for_subgoals(["a"])
    cs2 = mark_exhausted(cs, "a")
    a_crit = cs2[0]
    assert a_crit.verified is False
    assert a_crit.exhausted is True
    assert a_crit.tag == EXHAUSTED_TAG
    assert CriterionSource.RECOVERY.value in a_crit.sources
    assert a_crit.to_criterion().exhausted is True

    # Unknown key returns unchanged
    cs3 = mark_exhausted(cs, "bogus")
    assert cs3 == cs


def test_is_run_complete_and_decision():
    # All verified -> complete, fast_forward
    cs = criteria_for_subgoals(["a", "b"])
    cs = mark_verified(cs, "a", source=CriterionSource.GRADER)
    cs = mark_verified(cs, "b", source=CriterionSource.EXTERNAL)
    assert is_run_complete(cs) is True
    assert run_decision(cs)["decision"] == "FAST_FORWARD"

    # One pending -> incomplete, revert
    cs2 = criteria_for_subgoals(["a", "b"])
    cs2 = mark_verified(cs2, "a", source=CriterionSource.GRADER)
    assert is_run_complete(cs2) is False
    assert run_decision(cs2)["decision"] == "REVERT"

    # Two verified + one exhausted -> complete, fast_forward
    cs3 = criteria_for_subgoals(["a", "b", "c"])
    cs3 = mark_verified(cs3, "a", source=CriterionSource.GRADER)
    cs3 = mark_verified(cs3, "b", source=CriterionSource.EXTERNAL)
    cs3 = mark_exhausted(cs3, "c")
    assert is_run_complete(cs3) is True
    assert run_decision(cs3)["decision"] == "FAST_FORWARD"

    # Empty list -> not complete
    assert is_run_complete([]) is False


def test_could_not_verify_and_pending():
    cs = criteria_for_subgoals(["a", "b", "c"])
    cs = mark_verified(cs, "a", source=CriterionSource.GRADER)
    cs = mark_exhausted(cs, "c")
    cnv = could_not_verify(cs)
    pnd = pending(cs)
    assert {c.key for c in cnv} == {"b", "c"}
    assert {c.key for c in pnd} == {"b"}
    # The exhausted criterion in cnv carries EXHAUSTED_TAG
    c_cnv = next(c for c in cnv if c.key == "c")
    assert c_cnv.tag == EXHAUSTED_TAG
    assert c_cnv.exhausted is True


def test_apply_recovery_surface_exhausts():
    cs = criteria_for_subgoals(["a"])
    # Surface directive
    d = RecoveryDirective(
        action=RecoveryAction.ABORT,
        failure=FailureType.IMPOSSIBLE,
        decision=RecoveryDecision.SURFACE,
        status_tag=EXHAUSTED_TAG,
        surfaced=True,
        reason="test",
    )
    cs2 = apply_recovery(cs, "a", d)
    a_crit = cs2[0]
    assert a_crit.exhausted is True
    assert a_crit.tag == EXHAUSTED_TAG

    # RE_BRANCH directive -> unchanged
    d_branch = RecoveryDirective(
        action=RecoveryAction.RE_BRANCH,
        failure=FailureType.NO_STATE_CHANGE,
        decision=RecoveryDecision.RE_BRANCH,
        surfaced=False,
        reason="branch",
    )
    cs3 = apply_recovery(cs, "a", d_branch)
    a_crit3 = cs3[0]
    assert a_crit3.exhausted is False
    assert a_crit3.verified is False

    # NONE directive -> unchanged
    d_none = RecoveryDirective(
        action=RecoveryAction.NONE,
        failure=FailureType.NONE,
        decision=RecoveryDecision.NONE,
        reason="none",
    )
    cs4 = apply_recovery(cs, "a", d_none)
    a_crit4 = cs4[0]
    assert a_crit4.exhausted is False
    assert a_crit4.verified is False


def test_grade_criterion_holds():
    passed, res = asyncio.run(
        grade_criterion(
            subgoal="the Save button is present",
            result_state="<button>Save</button> visible and enabled",
            send=fake_send('{"verdict":"holds","reasons":["present"]}'),
        )
    )
    assert passed is True
    assert res.decorrelated is True
    assert is_decorrelated(HOLO_BACKEND, res.critic_backend) is True
    assert family_of(res.critic_backend) != "holo"


def test_grade_criterion_failsafe():
    for reply in ('{"verdict":"violated"}', '{"verdict":"unknown"}', "garbage not json"):
        passed, _ = asyncio.run(
            grade_criterion(
                subgoal="x",
                result_state="y",
                send=fake_send(reply),
            )
        )
        assert passed is False

    # Raising send
    passed, _ = asyncio.run(
        grade_criterion(
            subgoal="x",
            result_state="y",
            send=raising_send(),
        )
    )
    assert passed is False


def test_grade_criterion_same_family_rejected():
    with pytest.raises(PostConditionError):
        asyncio.run(
            grade_criterion(
                subgoal="x",
                result_state="y",
                actor_backend="deepseek_v4_flash",
                critic_backend="deepseek_v4_flash",
                send=fake_send('{"verdict":"holds"}'),
            )
        )


def test_make_grader_sync():
    g = make_grader(send=fake_send('{"verdict":"holds"}'))
    assert g("s", "state") is True

    g_false = make_grader(send=fake_send('{"verdict":"violated"}'))
    assert g_false("s", "state") is False

    g_raise = make_grader(send=raising_send())
    assert g_raise("s", "state") is False


def test_default_actor_is_holo_decorrelated():
    # Verify that decorrelated critic for HOLO_BACKEND is not in "holo" family
    crit = decorrelated_critic_backend(HOLO_BACKEND)
    assert family_of(crit) != "holo"
    assert is_decorrelated(HOLO_BACKEND, crit) is True

    # actual grade_criterion with default actor
    passed, res = asyncio.run(
        grade_criterion(
            subgoal="x",
            result_state="y",
            send=fake_send('{"verdict":"holds"}'),
        )
    )
    assert passed is True
    assert family_of(res.critic_backend) != "holo"


def test_compose_not_duplicate():
    text = (Path(__file__).resolve().parents[2] / "core/gui/termination.py").read_text()
    assert "from core.verify.termination import" in text
    assert "is_complete" in text
    assert "termination_decision" in text
    assert "verify_post_condition" in text
    assert "decorrelated_critic_backend" in text
    assert "def merge_decision" not in text
    assert "def is_fast_forwardable" not in text
    assert "FAMILY_BY_BACKEND" not in text


def test_import_purity():
    src = Path(__file__).resolve().parents[2] / "core/gui/termination.py"
    text = src.read_text()
    forbidden = ["core.backends", "core.nodes", "aiohttp", "requests", "httpx", "import docker"]
    for keyword in forbidden:
        assert keyword not in text

    # Subprocess probe
    env = os.environ.copy()
    env["PYTHONPATH"] = "."  # relative to bobclaw-core (cwd)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import core.gui.termination, sys; print(any(m=='core.backends' or m=='core.nodes' for m in sys.modules))",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "False"


def test_exhausted_all_surfaced():
    cs = criteria_for_subgoals(["a", "b"])
    cs = mark_exhausted(cs, "a")
    cs = mark_exhausted(cs, "b")
    assert is_run_complete(cs) is True
    assert run_decision(cs)["decision"] == "FAST_FORWARD"
    cnv = could_not_verify(cs)
    assert {c.key for c in cnv} == {"a", "b"}
    for c in cnv:
        assert c.tag == EXHAUSTED_TAG
