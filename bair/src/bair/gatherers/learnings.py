"""Load learnings from YAML into a hierarchical text block."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from ..domain.models import PolicyEntry
from ..infra.constants import LEARNINGS_CONTEXT, TEMPLATES_DIR
from ..contracts import ActionsIO, FileStore
from ..log import logger


@lru_cache(maxsize=1)
def _load_template() -> str:
    return (TEMPLATES_DIR / "learnings_header.md").read_text(encoding="utf-8")


def _format_entry(e: PolicyEntry) -> str:
    lines = [f"[{e.type}] {e.rule}", f"  -> When: {e.boundary}"]
    if e.confidence:
        lines.append(f"  -> Confidence: {e.confidence}")
    return "\n".join(lines)


def _parse_learnings(learnings_file: Path) -> list[PolicyEntry]:
    """Parse YAML and return active entries with rule + boundary."""
    data = yaml.safe_load(learnings_file.read_text(encoding="utf-8"))
    raw = data if isinstance(data, list) else data.get("learnings", []) if isinstance(data, dict) else []

    entries = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("active", True):
            continue
        rule = item.get("rule", "")
        boundary = item.get("decision_boundary", "")
        if rule and boundary:
            entries.append(PolicyEntry(
                type=item.get("type", "unknown"),
                rule=rule,
                boundary=boundary,
                confidence=item.get("confidence", ""),
            ))
    return entries


def gather_learnings(variant: str, store: FileStore, actions: ActionsIO) -> str:
    """Read ``learnings/{variant}.yml`` and return formatted text."""
    learnings_file = Path(f"learnings/{variant}.yml")

    if not learnings_file.exists():
        logger.debug(f"No learnings file for variant '{variant}' -- skipping")
        store.write(LEARNINGS_CONTEXT, "")
        return ""

    entries = _parse_learnings(learnings_file)

    if not entries:
        logger.debug("No learnings found (empty list)")
        store.write(LEARNINGS_CONTEXT, "")
        return ""

    logger.debug(f"Loaded {len(entries)} policy entries")
    items = "\n\n".join(_format_entry(e) for e in entries)
    text = _load_template().format(entries=items)
    store.write(LEARNINGS_CONTEXT, text)
    return text
