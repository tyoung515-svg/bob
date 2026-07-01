from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2

from core.memory.exceptions import MemoryConfigError, RenderFailed

if TYPE_CHECKING:
    from core.memory.interfaces import FactStore
    from core.memory.models import Section


class JinjaRenderer:
    def __init__(
        self,
        template_dir: Path,
        section_mapping_path: Path,
    ) -> None:
        self._template_dir = template_dir
        if not template_dir.is_dir():
            raise MemoryConfigError(
                f"template directory not found: {template_dir}"
            )
        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(template_dir)),
            autoescape=False,
        )
        raw = section_mapping_path.read_text(encoding="utf-8")
        import tomllib
        parsed = tomllib.loads(raw)
        self._templates: dict[str, str] = {}
        section_block = parsed.get("section", {})
        for section_id, value in section_block.items():
            self._templates[section_id] = value.get("template", "section.j2")

    def render(
        self,
        sections: list[Section],
        output_dir: Path,
    ) -> list[Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for section in sections:
            template_name = self._templates.get(
                section.section_id, "section.j2"
            )
            try:
                template = self._env.get_template(template_name)
            except jinja2.TemplateNotFound as exc:
                raise RenderFailed(
                    section.section_id,
                    f"template {template_name!r} not found in {self._template_dir}",
                ) from exc

            try:
                rendered = template.render(section=section, facts=[])
            except jinja2.TemplateError as exc:
                raise RenderFailed(
                    section.section_id,
                    f"jinja2 rendering failed: {exc}",
                ) from exc

            out_path = output_dir / f"{section.section_id}.md"
            tmp_path = out_path.with_suffix(".md.tmp")
            try:
                tmp_path.write_text(rendered, encoding="utf-8")
                with tmp_path.open("ab") as f:
                    import os
                    f.flush()
                    os.fsync(f.fileno())
                tmp_path.rename(out_path)
            except OSError as exc:
                raise RenderFailed(
                    section.section_id,
                    f"write failed: {exc}",
                ) from exc

            written.append(out_path)

        return written
