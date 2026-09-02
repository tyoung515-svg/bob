from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.memory._hashing import blake3_hex, canonical_json
from core.watch.run import WatchRunResult

__all__ = [
    "DIGEST_KIND",
    "DIGEST_VERSION",
    "SEVERITY_ORDER",
    "LIVE_CORPUS_ROOTS",
    "WatchEvent",
    "AlertThreshold",
    "AlertFiring",
    "DigestSection",
    "DigestArtifact",
    "to_events",
    "thresholds_from_dicts",
    "_matches",
    "evaluate_thresholds",
    "build_digest",
    "digest_from_run",
    "persist_digest",
]

DIGEST_KIND = "watch_digest"
DIGEST_VERSION = 0
SEVERITY_ORDER: dict[str, int] = {"info": 0, "notable": 1, "alert": 2}
LIVE_CORPUS_ROOTS: tuple[Path, ...] = (
    Path("C:/dev/unwinding"),
    Path("C:/dev/wiki"),
    Path("C:/dev/sps"),
    Path("C:/dev/music-maker"),
)


@dataclass(frozen=True)
class WatchEvent:
    id: str
    title: str
    url: str | None = None
    source: str | None = None
    category: str = "update"
    severity: str = "info"
    summary: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("id must be non-empty after strip")
        if self.severity not in SEVERITY_ORDER:
            allowed = ", ".join(SEVERITY_ORDER.keys())
            raise ValueError(f"severity must be one of {{{allowed}}}, got {self.severity!r}")


@dataclass(frozen=True)
class AlertThreshold:
    name: str
    category: str | None = None
    source: str | None = None
    min_severity: str = "info"
    min_count: int = 1
    level: str = "notable"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("name must be non-empty")
        allowed = set(SEVERITY_ORDER.keys())
        if self.min_severity not in allowed:
            raise ValueError(f"min_severity must be one of {allowed}, got {self.min_severity!r}")
        if self.level not in allowed:
            raise ValueError(f"level must be one of {allowed}, got {self.level!r}")
        if self.min_count < 1:
            raise ValueError(f"min_count must be >= 1, got {self.min_count}")


@dataclass(frozen=True)
class AlertFiring:
    threshold: str
    level: str
    count: int
    event_ids: tuple[str, ...]


@dataclass(frozen=True)
class DigestSection:
    label: str
    events: tuple[WatchEvent, ...]


@dataclass(frozen=True)
class DigestArtifact:
    title: str
    profile: str | None
    new_count: int
    event_count: int
    group_by: str
    sections: tuple[DigestSection, ...]
    alerts: tuple[AlertFiring, ...]

    def to_markdown(self) -> str:
        lines: list[str] = []
        header = self.title if self.profile is None else f"{self.title} — {self.profile}"
        lines.append(f"# {header}")
        lines.append("")
        lines.append(f"_{self.new_count} new · {self.event_count} events · {len(self.alerts)} alerts_")
        if self.alerts:
            lines.append("")
            lines.append("## Alerts")
            for firing in self.alerts:
                ids = ", ".join(firing.event_ids)
                lines.append(f"- **{firing.threshold}** [{firing.level}] — {firing.count} events: {ids}")
        for section in self.sections:
            lines.append("")
            lines.append(f"## {section.label}")
            for evt in section.events:
                line = f"- **{evt.title}** [{evt.severity}]"
                meta_parts = []
                if evt.source:
                    meta_parts.append(evt.source)
                if evt.url:
                    meta_parts.append(evt.url)
                if meta_parts:
                    line += " · " + " · ".join(meta_parts)
                lines.append(line)
                if evt.summary:
                    lines.append(f"  {evt.summary}")
        # join with newline and add trailing newline
        return "\n".join(lines) + "\n"

    def to_doc(self) -> dict:
        body = {
            "kind": DIGEST_KIND,
            "version": DIGEST_VERSION,
            "title": self.title,
            "profile": self.profile,
            "group_by": self.group_by,
            "new_count": self.new_count,
            "event_count": self.event_count,
            "alerts": [
                {
                    "threshold": a.threshold,
                    "level": a.level,
                    "count": a.count,
                    "event_ids": list(a.event_ids),
                }
                for a in self.alerts
            ],
            "sections": [
                {
                    "label": s.label,
                    "events": [
                        {
                            "id": e.id,
                            "title": e.title,
                            "url": e.url,
                            "source": e.source,
                            "category": e.category,
                            "severity": e.severity,
                            "summary": e.summary,
                        }
                        for e in s.events
                    ],
                }
                for s in self.sections
            ],
        }
        digest_id = blake3_hex(canonical_json(body))
        return {"digest_id": digest_id, **body}

    @property
    def digest_id(self) -> str:
        return self.to_doc()["digest_id"]


def to_events(items: Iterable, *, category: str = "update", severity: str = "info") -> list[WatchEvent]:
    """Normalize an iterable of item MAPPINGS to WatchEvents."""
    result: list[WatchEvent] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise TypeError("each item must be a mapping (dict-like)")
        try:
            event_id = item["id"]
        except KeyError:
            raise ValueError("each item must have an 'id' key")
        title = item.get("title", "")
        url = item.get("url")
        source = item.get("source")
        item_cat = item.get("category", category)
        item_sev = item.get("severity", severity)
        summary = item.get("summary")
        result.append(
            WatchEvent(
                id=event_id,
                title=title,
                url=url,
                source=source,
                category=item_cat,
                severity=item_sev,
                summary=summary,
            )
        )
    return result


def thresholds_from_dicts(rows: Iterable[dict]) -> list[AlertThreshold]:
    valid_keys = {"name", "category", "source", "min_severity", "min_count", "level"}
    thrs: list[AlertThreshold] = []
    for row in rows:
        extra = set(row.keys()) - valid_keys
        if extra:
            raise ValueError(f"unknown keys: {extra}")
        thrs.append(AlertThreshold(**row))
    return thrs


def _matches(event: WatchEvent, th: AlertThreshold) -> bool:
    if th.category is not None and event.category != th.category:
        return False
    if th.source is not None and event.source != th.source:
        return False
    if SEVERITY_ORDER[event.severity] < SEVERITY_ORDER[th.min_severity]:
        return False
    return True


def evaluate_thresholds(events: Iterable[WatchEvent], thresholds: Iterable[AlertThreshold]) -> list[AlertFiring]:
    events_list = list(events)
    firings: list[AlertFiring] = []
    for th in thresholds:
        matching: list[WatchEvent] = [e for e in events_list if _matches(e, th)]
        if len(matching) >= th.min_count:
            firings.append(
                AlertFiring(
                    threshold=th.name,
                    level=th.level,
                    count=len(matching),
                    event_ids=tuple(e.id for e in matching),
                )
            )
    return firings


def build_digest(
    events: Iterable[WatchEvent],
    *,
    thresholds: Iterable[AlertThreshold] | None = None,
    title: str = "Watch digest",
    profile: str | None = None,
    group_by: str = "category",
    new_count: int | None = None,
) -> DigestArtifact:
    if group_by not in ("category", "source"):
        raise ValueError("group_by must be 'category' or 'source'")
    events_list = list(events)
    # group
    groups: dict[str, list[WatchEvent]] = {}
    for evt in events_list:
        label = getattr(evt, group_by) or ""
        groups.setdefault(label, []).append(evt)
    # sort sections by label ascending
    sorted_labels = sorted(groups.keys())
    sections = tuple(
        DigestSection(label=lab, events=tuple(groups[lab])) for lab in sorted_labels
    )
    if new_count is None:
        new_count = len(events_list)
    if thresholds is None:
        thresholds = []
    alerts = evaluate_thresholds(events_list, thresholds)
    return DigestArtifact(
        title=title,
        profile=profile,
        new_count=new_count,
        event_count=len(events_list),
        group_by=group_by,
        sections=sections,
        alerts=tuple(alerts),
    )


def digest_from_run(
    result: WatchRunResult,
    *,
    thresholds: Iterable[AlertThreshold] | None = None,
    title: str = "Watch digest",
    profile: str | None = None,
    group_by: str = "category",
    category: str = "update",
    severity: str = "info",
) -> DigestArtifact:
    events = to_events(result.new, category=category, severity=severity)
    return build_digest(
        events,
        thresholds=thresholds,
        title=title,
        profile=profile,
        group_by=group_by,
        new_count=result.new_count,
    )


def _is_within_live_corpus(path: Path) -> bool:
    """Check if `path` is inside any LIVE_CORPUS_ROOTS using case-insensitive containment."""
    resolved = os.path.normcase(os.path.normpath(str(path.resolve())))
    for root in LIVE_CORPUS_ROOTS:
        root_norm = os.path.normcase(os.path.normpath(str(root.resolve())))
        # Check resolved path starts with root_norm (plus separator) or equals root_norm
        if resolved == root_norm or resolved.startswith(root_norm + os.sep):
            return True
    return False


def persist_digest(artifact: DigestArtifact, path: str | Path, *, overwrite: bool = False) -> Path:
    path = Path(path).resolve()
    if _is_within_live_corpus(path):
        raise ValueError(f"refusing to write into a live corpus: {path}")
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists and overwrite=False")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = artifact.to_doc()
    with path.open("w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False, sort_keys=False)
    return path
