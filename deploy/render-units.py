#!/usr/bin/env python3
"""Render user unit files to a staging directory; never install or start them."""
from __future__ import annotations

import argparse
from pathlib import Path


def _escape(value: str, *, exec_start: bool = False) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("paths must not contain control characters")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return value.replace("$", "$$") if exec_start else value


def render(bob_home: Path, env_file: Path, output_dir: Path) -> list[Path]:
    values = {"@BOB_HOME@": str(bob_home.resolve()), "@BOB_ENV_FILE@": str(env_file.resolve())}
    templates = sorted((Path(__file__).parent / "systemd").glob("*.service.in"))
    rendered = []
    for template in templates:
        lines = []
        for line in template.read_text(encoding="utf-8").splitlines():
            for key, value in values.items():
                line = line.replace(key, _escape(value, exec_start=line.startswith("ExecStart=")))
            lines.append(line)
        rendered.append((output_dir / template.stem, "\n".join(lines) + "\n"))
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, content in rendered:
        path.write_text(content, encoding="utf-8")
    return [path for path, _ in rendered]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bob-home", required=True, type=Path, help="checkout containing .venv")
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        for path in render(args.bob_home, args.env_file, args.output_dir):
            print(path)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"render-units: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
