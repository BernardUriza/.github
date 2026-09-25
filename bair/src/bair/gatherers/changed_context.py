"""changed_context gatherer — the code around a diff, so the gatekeeper can follow
what the change feeds.

A hunk shows the lines that changed, not the code that reads their result. In
server-bot#104 (backlog 01, 2026-09-25) an ``all`` → ``any`` flip in ``_payload``
made a stored row crash ``_expired`` on resume; ``_expired`` lives in the same
file, outside the hunk, and the gatekeeper never saw it. This gatherer adds two
things to the review payload, both read from the PR-head checkout:

  - the full post-change text of every file the diff touches (``FILE: <path>``);
  - the call sites of every function the diff touches, across the repo
    (``CALLERS OF <name>``), found with ``git grep``.

Bounded by a per-file and a total byte budget, source files before tests. Fail-soft:
no checkout, no git, or nothing readable returns ``""`` and the review falls back
to the diff alone. Pure stdlib (no xair) so it is unit-testable in isolation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_MAX_BYTES = 80_000
_MAX_FILE_BYTES = 40_000
_MAX_CALLER_BYTES = 20_000
_MAX_NAMES = 12

_SKIP_SUFFIXES = (".lock", ".min.js", ".map", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf")
_SKIP_NAMES = ("package-lock.json", "poetry.lock", "uv.lock", "yarn.lock", "pnpm-lock.yaml")

_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|function|func|fn)\s+([A-Za-z_]\w*)")
_LINE_DEF_RE = re.compile(r"^[+-]\s*(?:async\s+)?(?:def|function|func|fn)\s+([A-Za-z_]\w*)", re.MULTILINE)


def changed_files(diff: str) -> list[str]:
    """Paths the diff adds or modifies (deleted files have no ``+++ b/``)."""
    seen: dict[str, None] = {}
    for path in _FILE_RE.findall(diff):
        path = path.strip()
        if path.endswith(_SKIP_SUFFIXES) or Path(path).name in _SKIP_NAMES:
            continue
        seen.setdefault(path, None)
    return list(seen)


def _changed_lines(diff: str) -> dict[str, list[int]]:
    """Post-change line numbers each file's hunks touch: every added line, or the
    hunk's anchor line when it only deletes."""
    lines: dict[str, list[int]] = {}
    path, new_no, added = "", 0, False
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:].strip() if raw.startswith("+++ b/") else ""
            continue
        m = _HUNK_RE.match(raw)
        if m:
            if path and new_no and not added:
                lines.setdefault(path, []).append(max(new_no - 1, 1))
            new_no, added = int(m.group(1)), False
            continue
        if not path or not new_no or raw.startswith(("diff ", "--- ", "index ")):
            continue
        if raw.startswith("+"):
            lines.setdefault(path, []).append(new_no)
            added = True
            new_no += 1
        elif raw.startswith(" "):
            new_no += 1
    if path and new_no and not added:
        lines.setdefault(path, []).append(max(new_no - 1, 1))
    return lines


def _enclosing(text: str, line_no: int) -> str:
    """Name of the nearest function definition at or above ``line_no`` (1-based)."""
    src = text.splitlines()
    for i in range(min(line_no, len(src)) - 1, -1, -1):
        m = _DEF_RE.match(src[i])
        if m:
            return m.group(1)
    return ""


def changed_functions(diff: str, root: str | Path | None = None) -> list[str]:
    """Functions the diff touches: the ones enclosing each changed line in the
    post-change file (git's hunk header names the def BEFORE the hunk, not the one
    changed), plus any function a +/- line defines."""
    names: list[str] = []
    if root is not None:
        for rel, nos in _changed_lines(diff).items():
            text = _read(Path(root), rel)
            names.extend(_enclosing(text, n) for n in nos)
    names.extend(_LINE_DEF_RE.findall(diff))
    seen: dict[str, None] = {}
    for name in names:
        if len(name) >= 3 and not name.startswith("__") and not name.startswith("test"):
            seen.setdefault(name, None)
    return list(seen)[:_MAX_NAMES]


def _is_test(path: str) -> bool:
    parts = Path(path).parts
    return any(p in ("tests", "test", "__tests__") for p in parts) or Path(path).name.startswith("test_")


def _read(root: Path, rel: str) -> str:
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) > _MAX_FILE_BYTES:
        text = text[:_MAX_FILE_BYTES] + f"\n… [truncated at {_MAX_FILE_BYTES} bytes]"
    return text


def _callers(root: Path, name: str) -> str:
    try:
        out = subprocess.run(
            ["git", "grep", "-n", "-w", "-C2", "--", name],
            cwd=root, capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    if len(out) > _MAX_CALLER_BYTES:
        out = out[:_MAX_CALLER_BYTES] + f"\n… [truncated at {_MAX_CALLER_BYTES} bytes]"
    return out.strip()


def gather_changed_context(diff: str, root: str | Path = ".") -> str:
    """The ``<changed_code_context>`` block for ``diff``, or ``""`` when nothing fits."""
    root = Path(root)
    if not diff or not root.is_dir():
        return ""
    files = sorted(changed_files(diff), key=_is_test)
    sections: list[str] = []
    budget = _MAX_BYTES
    for rel in files:
        text = _read(root, rel)
        if not text:
            continue
        section = f"FILE: {rel} (full text after the change)\n{text}"
        if len(section) > budget:
            sections.append(f"FILE: {rel} — omitted, context budget exhausted")
            continue
        sections.append(section)
        budget -= len(section)
    for name in changed_functions(diff, root):
        hits = _callers(root, name)
        if not hits:
            continue
        section = f"CALLERS OF {name} (git grep -w, whole repo)\n{hits}"
        if len(section) > budget:
            sections.append(f"CALLERS OF {name} — omitted, context budget exhausted")
            continue
        sections.append(section)
        budget -= len(section)
    if not sections:
        return ""
    return "<changed_code_context>\n" + "\n\n".join(sections) + "\n</changed_code_context>"
