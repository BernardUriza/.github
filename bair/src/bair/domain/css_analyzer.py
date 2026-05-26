"""Pure CSS static analysis — no I/O, no protocols."""

from __future__ import annotations

import re
from dataclasses import dataclass

_LAYER_RE = re.compile(r"@layer\s+components\s*\{")
_SELECTOR_RE = re.compile(r"\.([a-zA-Z][-a-zA-Z0-9_]*)\s*\{")


@dataclass
class CssFinding:
    kind: str  # "same file" | "cross-file"
    selector: str
    detail: str

    def format(self) -> str:
        return f"DUPLICATE ({self.kind}): `.{self.selector}` {self.detail}"


def extract_selectors(content: str) -> list[tuple[str, int]]:
    """Extract CSS class selectors — single pass, @layer-aware.

    Handles both @layer components blocks and top-level selectors.
    Deduplicates by name (first occurrence wins).
    """
    selectors: list[tuple[str, int]] = []
    seen: set[str] = set()
    in_layer = False
    brace_depth = 0

    for line_num, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()

        if _LAYER_RE.match(stripped):
            in_layer = True
            brace_depth = 1
            continue

        if in_layer:
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0:
                in_layer = False
                continue

        match = _SELECTOR_RE.match(stripped)
        if match:
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                selectors.append((name, line_num))

    return selectors


def find_duplicates(file_selectors: dict[str, list[tuple[str, int]]]) -> list[CssFinding]:
    """Find within-file and cross-file duplicate selectors."""
    findings: list[CssFinding] = []

    for path, sels in file_selectors.items():
        seen: dict[str, list[int]] = {}
        for name, line in sels:
            seen.setdefault(name, []).append(line)
        for name, lines in seen.items():
            if len(lines) > 1:
                line_list = ", ".join(str(ln) for ln in lines)
                findings.append(CssFinding(
                    kind="same file",
                    selector=name,
                    detail=f"defined {len(lines)} times in `{path}` at lines {line_list}",
                ))

    all_selectors: dict[str, list[str]] = {}
    for path, sels in file_selectors.items():
        for name, _ in sels:
            all_selectors.setdefault(name, []).append(path)
    for name, files in all_selectors.items():
        unique_files = sorted(set(files))
        if len(unique_files) > 1:
            file_list = ", ".join(f"`{f}`" for f in unique_files)
            findings.append(CssFinding(
                kind="cross-file",
                selector=name,
                detail=f"defined in {len(unique_files)} files: {file_list}",
            ))

    return findings
