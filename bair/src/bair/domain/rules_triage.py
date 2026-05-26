"""Pure rule selection — no I/O, no protocols."""

from __future__ import annotations

import re

from .invariants import ALWAYS_ON_RULES

# Extension → extra rules mapping
_EXTENSION_RULES: list[tuple[str, set[str]]] = [
    (r"\.tsx$", {"code-quality.md", "styling.md", "data-ref.md"}),
    (r"\.ts$", {"code-quality.md"}),
    (r"\.css$|styles/", {"styling.md"}),
    (r"\.(service|controller|guard|module|interceptor)\.ts$", {"multi-tenancy.md", "nestjs.md"}),
]


def select_rules(changed_files: str) -> list[str]:
    """Pick rule files based on changed file extensions. Pure function."""
    selected = set(ALWAYS_ON_RULES)
    for pattern, rules in _EXTENSION_RULES:
        if re.search(pattern, changed_files, re.MULTILINE):
            selected.update(rules)
    return sorted(selected)
