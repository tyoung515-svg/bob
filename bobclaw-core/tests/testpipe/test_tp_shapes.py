"""Test-pipe — SHAPES registry (SPEC §5, §9 unit 6).

Proves the registry ships the three shapes, they are selectable by name, they REUSE
the existing nodes (identity, not shadow copies), and a new shape registers with no
harness change.
"""
from __future__ import annotations

import pytest

import core.nodes.council as council_mod
import core.nodes.debate as debate_mod
import core.nodes.panel as panel_mod
import core.nodes.synthesize as synth_mod
from core.testpipe import shapes
from core.testpipe.shapes import ShapeSpec, get_shape, register, shape_names


def test_registry_ships_the_three_shapes():
    assert set(shape_names()) >= {"fusion", "sequential", "debate"}


def test_shapes_reuse_the_existing_nodes_by_identity():
    # The registry holds the LIVE node callables, not copies (SPEC §5).
    assert get_shape("fusion").dispatch_node is panel_mod.panel_dispatch_node
    assert get_shape("fusion").seat_node is panel_mod.panel_worker_node
    assert get_shape("fusion").close_node is synth_mod.synthesize_node
    assert get_shape("sequential").sequential_node is council_mod.council_node
    assert get_shape("debate").close_node is debate_mod.debate_converge_node
    assert get_shape("debate").seat_node is panel_mod.panel_worker_node


def test_build_spec_carries_mode_and_params():
    spec = get_shape("debate").build_spec(["framer", "stress"])
    assert spec["mode"] == "debate" and spec["seats"] == ["framer", "stress"]
    assert spec["max_rounds"] == 3        # debate param flows through
    fusion_spec = get_shape("fusion").build_spec(["framer"])
    assert fusion_spec["mode"] == "fusion" and "max_rounds" not in fusion_spec


def test_get_shape_unknown_is_loud():
    with pytest.raises(KeyError):
        get_shape("tournament")


def test_register_a_new_shape_makes_it_selectable(monkeypatch):
    # Adding a future shape = one register() call → selectable, no harness edit (SPEC §5).
    monkeypatch.setitem(shapes.SHAPES, "tournament",
                        ShapeSpec(name="tournament", mode="fusion",
                                  dispatch_node=panel_mod.panel_dispatch_node,
                                  seat_node=panel_mod.panel_worker_node))
    assert "tournament" in shape_names()
    assert get_shape("tournament").mode == "fusion"


def test_register_rejects_duplicate_without_overwrite():
    with pytest.raises(ValueError):
        register(ShapeSpec(name="fusion", mode="fusion"))
