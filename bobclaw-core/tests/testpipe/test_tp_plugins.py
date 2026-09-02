"""Test-pipe — EvalPlus plugin shells (SPEC §9 unit 1, §11).

MS5-P1 scope: the plugin INTERFACE is what's proven tonight; the dataset load is
lazy/guarded (``evalplus`` is not a bobclaw dependency), so ``load`` raises a clear
:class:`EvalPlusNotAvailable` here (the LIVE sweep is Travis's, deferred).
"""
from __future__ import annotations

import importlib.util

import pytest

from core.testpipe.loader import BenchmarkPlugin
from core.testpipe.plugins import EvalPlusNotAvailable
from core.testpipe.plugins.humaneval_plus import HumanEvalPlusPlugin
from core.testpipe.plugins.mbpp_plus import MbppPlusPlugin
from core.testpipe.types import SPINE_BUILD

_HAS_EVALPLUS = importlib.util.find_spec("evalplus") is not None


@pytest.mark.parametrize("Plugin,name", [
    (HumanEvalPlusPlugin, "humaneval_plus"),
    (MbppPlusPlugin, "mbpp_plus"),
])
def test_plugin_conforms_to_interface(Plugin, name):
    plugin = Plugin()
    assert isinstance(plugin, BenchmarkPlugin)       # runtime Protocol conformance
    assert plugin.name == name and plugin.spine == SPINE_BUILD


@pytest.mark.skipif(_HAS_EVALPLUS, reason="evalplus installed — load path is live")
@pytest.mark.parametrize("Plugin", [HumanEvalPlusPlugin, MbppPlusPlugin])
def test_load_without_evalplus_raises_clear_error(Plugin):
    with pytest.raises(EvalPlusNotAvailable) as exc:
        Plugin().load()
    assert "evalplus" in str(exc.value).lower()       # actionable install hint
