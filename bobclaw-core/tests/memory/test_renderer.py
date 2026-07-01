from __future__ import annotations

from pathlib import Path

import pytest

from core.memory.exceptions import MemoryConfigError, RenderFailed
from core.memory.models import Section


_VALID_TOML = """
[meta]
spec_version = "1.0"

[section.test_sec]
title = "Test Section"
predicate = { generation_method = "test" }
template = "section.j2"
"""


@pytest.fixture
def template_dir(tmp_path: Path) -> Path:
    d = tmp_path / "templates"
    d.mkdir()
    (d / "section.j2").write_text(
        "# {{ section.title }}\n\nid: {{ section.section_id }}\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def mapping_file(tmp_path: Path) -> Path:
    p = tmp_path / "mapping.toml"
    p.write_text(_VALID_TOML, encoding="utf-8")
    return p


@pytest.fixture
def sections() -> list[Section]:
    return [
        Section(
            section_id="test_sec",
            title="Test Section",
            fact_ids=["f1"],
            spec_version="1.0",
            input_hash="abc",
        ),
    ]


@pytest.fixture
def renderer(template_dir, mapping_file):
    from core.memory.renderer import JinjaRenderer
    return JinjaRenderer(template_dir, mapping_file)


class TestConstruction:
    def test_constructs(self, renderer):
        assert renderer is not None

    def test_rejects_missing_template_dir(self, tmp_path):
        from core.memory.renderer import JinjaRenderer
        with pytest.raises(MemoryConfigError):
            JinjaRenderer(tmp_path / "nonexistent", tmp_path / "dummy.toml")


class TestRender:
    def test_renders_section_to_markdown(
        self, renderer, sections, tmp_path
    ):
        output_dir = tmp_path / "output"
        written = renderer.render(sections, output_dir)
        assert len(written) == 1
        assert written[0] == output_dir / "test_sec.md"
        content = written[0].read_text(encoding="utf-8")
        assert "# Test Section" in content
        assert "test_sec" in content

    def test_rerender_produces_bit_identical_output(
        self, renderer, sections, tmp_path
    ):
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        r1 = renderer.render(sections, out1)
        r2 = renderer.render(sections, out2)
        assert r1[0].read_text(encoding="utf-8") == r2[0].read_text(encoding="utf-8")

    def test_atomic_write_no_tmp_left(
        self, renderer, sections, tmp_path
    ):
        output_dir = tmp_path / "output"
        renderer.render(sections, output_dir)
        assert not list(output_dir.glob("*.tmp"))
        assert (output_dir / "test_sec.md").exists()

    def test_raises_on_missing_template(
        self, template_dir, mapping_file, sections, tmp_path
    ):
        from core.memory.renderer import JinjaRenderer
        (template_dir / "section.j2").unlink()
        r = JinjaRenderer(template_dir, mapping_file)
        with pytest.raises(RenderFailed, match="template"):
            r.render(sections, tmp_path / "out")

    def test_output_path_matches_section_id(
        self, renderer, sections, tmp_path
    ):
        written = renderer.render(sections, tmp_path / "wiki")
        assert written[0].name == "test_sec.md"
