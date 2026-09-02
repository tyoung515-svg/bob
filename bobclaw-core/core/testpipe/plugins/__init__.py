"""Test-pipe benchmark plugins (SPEC §9 unit 1, §11).

Each plugin implements :class:`~core.testpipe.loader.BenchmarkPlugin`. The EvalPlus
plugins import the ``evalplus`` package LAZILY (only inside ``load``), so importing
this package never requires the dataset dependency — the INTERFACE is what MS5-P1
proves; the live dataset load is the deferred live sweep (SPEC §10, Travis's)."""
from __future__ import annotations


class EvalPlusNotAvailable(RuntimeError):
    """Raised when a plugin's ``load`` runs but the ``evalplus`` package is absent.

    Carries the install hint so the deferred live sweep has a clear next step."""
