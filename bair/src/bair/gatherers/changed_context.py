"""changed_context gatherer — the code around a diff, so the gatekeeper can follow
what the change feeds.

A hunk shows the lines that changed, not the code that reads their result. In
server-bot#104 (backlog 01, 2026-09-25) an ``all`` → ``any`` flip in ``_payload``
made a stored row crash ``_expired`` on resume; ``_expired`` lives in the same
file, outside the hunk, and the gatekeeper never saw it. This gatherer adds the
code the change feeds to the review payload, read from the PR-head checkout, in one
of three modes (``BAIR_CONTEXT``, default ``slice`` — chosen by the eval suite):

  - ``slice``: function bodies along the data flow — the functions enclosing each
    changed line, their callers, the same-module functions that touch the data
    keys the hunks touch, and one hop into same-file helpers (``FUNCTION ...``);
  - ``full``: the full post-change text of every touched file (``FILE: <path>``)
    plus ``git grep`` call sites of every changed function (``CALLERS OF <name>``);
  - ``none``: nothing.

Bounded by byte budgets, source before tests. Fail-soft: no checkout, no git, or
nothing readable returns ``""`` and the review falls back to the diff alone. Pure
stdlib (no xair) so it is unit-testable in isolation.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_MAX_BYTES = 80_000
_MAX_FILE_BYTES = 40_000
_MAX_CALLER_BYTES = 20_000
_MAX_NAMES = 12
_SLICE_BYTES = 30_000
_MAX_BODY_LINES = 120
_MAX_HITS = 15
_MAX_KEYS = 6
_MODE_ENV = "BAIR_CONTEXT"
_MODES = ("full", "slice", "none")

_SKIP_SUFFIXES = (".lock", ".min.js", ".map", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf")
_SKIP_NAMES = ("package-lock.json", "poetry.lock", "uv.lock", "yarn.lock", "pnpm-lock.yaml")

_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|function|func|fn)\s+([A-Za-z_]\w*)")
_LINE_DEF_RE = re.compile(r"^[+-]\s*(?:async\s+)?(?:def|function|func|fn)\s+([A-Za-z_]\w*)", re.MULTILINE)
_KEY_RE = re.compile(r"""["']([A-Za-z_][\w-]{2,40})["']""")
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


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


def _spans(text: str) -> list[tuple[str, int, int]]:
    """``(name, start, end)`` 0-based line spans of every function: a body ends at
    the next non-blank line indented no deeper than its ``def``."""
    src = text.splitlines()
    spans: list[tuple[str, int, int]] = []
    for i, line in enumerate(src):
        m = _DEF_RE.match(line)
        if not m:
            continue
        indent = len(line) - len(line.lstrip())
        end = i + 1
        while end < len(src):
            nxt = src[end]
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent and not nxt.lstrip().startswith((")", "]", "}")):
                break
            end += 1
        spans.append((m.group(1), i, end))
    return spans


def _innermost(spans: list[tuple[str, int, int]], line_no: int) -> tuple[str, int, int] | None:
    inside = [s for s in spans if s[1] <= line_no - 1 < s[2]]
    return min(inside, key=lambda s: s[2] - s[1]) if inside else None


def _grep(root: Path, pattern: str, *, fixed: bool = False) -> list[tuple[str, int]]:
    args = ["git", "grep", "-n", "-F" if fixed else "-w", "--", pattern]
    try:
        out = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    hits: list[tuple[str, int]] = []
    for line in out.splitlines()[:_MAX_HITS]:
        path, _, rest = line.partition(":")
        no, _, _ = rest.partition(":")
        if no.isdigit():
            hits.append((path, int(no)))
    return hits


def _hunk_keys(diff: str) -> list[str]:
    """String keys the hunks read or write (``data["attachments"]``) — the data the
    change touches, whose other readers the slice must include."""
    seen: dict[str, None] = {}
    for raw in diff.splitlines():
        if raw.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        for key in _KEY_RE.findall(raw[1:] if raw[:1] in "+- " else raw):
            seen.setdefault(key, None)
    return list(seen)[:_MAX_KEYS]


def _slice(diff: str, root: Path) -> str:
    """Function bodies, not files: the functions the diff changes, their callers,
    the functions that touch the same data keys, and one hop into same-file helpers
    those call. Approximates data-flow slicing, which beat diff-only review (37% vs
    24% key-bug inclusion) while raw context dumps degraded plain LLM reviews
    (AACR-Bench) — see BernardUriza/.github backlog 01."""
    texts: dict[str, str] = {}
    spans: dict[str, list[tuple[str, int, int]]] = {}

    def load(rel: str) -> bool:
        if rel not in texts:
            texts[rel] = _read(root, rel)
            spans[rel] = _spans(texts[rel])
        return bool(texts[rel])

    changed = _changed_lines(diff)
    # (file, function) -> (start, end, why, priority); lower priority renders first.
    picked: dict[tuple[str, str], tuple[int, int, str, int]] = {}

    def take(rel: str, line_no: int, why: str, prio: int) -> None:
        if not load(rel):
            return
        span = _innermost(spans[rel], line_no)
        if span and (rel, span[0]) not in picked:
            picked[(rel, span[0])] = (span[1], span[2], why, prio)

    for rel, nos in changed.items():
        for n in nos:
            take(rel, n, "changed", 0)
    for name in changed_functions(diff, root):
        for rel, n in _grep(root, name):
            same = rel in changed
            if not same and _is_test(rel):
                continue
            load(rel)
            if _DEF_RE.match(texts[rel].splitlines()[n - 1] if 0 < n <= len(texts[rel].splitlines()) else ""):
                continue
            take(rel, n, f"calls {name}", 1 if same else 3)
    # Readers of the same data keys, within the module that owns them: the
    # contract between a row writer and its readers lives next to both.
    for key in _hunk_keys(diff):
        for rel in changed:
            if not load(rel):
                continue
            for i, line in enumerate(texts[rel].splitlines(), 1):
                if f'"{key}"' in line or f"'{key}'" in line:
                    take(rel, i, f"touches '{key}'", 2 if _is_test(rel) else 1)
    for (rel, owner), (start, end, _why, prio) in list(picked.items()):
        body = "\n".join(texts[rel].splitlines()[start:end])
        local = {sp[0]: sp for sp in spans[rel]}
        for called in dict.fromkeys(_CALL_RE.findall(body)):
            if called in local and (rel, called) not in picked:
                picked[(rel, called)] = (local[called][1], local[called][2], f"called by {owner}", prio + 1)

    sections: list[str] = []
    budget = _SLICE_BYTES
    ordered = sorted(picked.items(), key=lambda kv: (kv[1][3], _is_test(kv[0][0])))
    for (rel, name), (start, end, why, _prio) in ordered:
        lines = texts[rel].splitlines()[start:end]
        if len(lines) > _MAX_BODY_LINES:
            lines = [*lines[:_MAX_BODY_LINES], f"… [{end - start - _MAX_BODY_LINES} more lines]"]
        section = f"FUNCTION {rel}::{name} (lines {start + 1}-{end}, {why})\n" + "\n".join(lines)
        if len(section) > budget:
            sections.append(f"FUNCTION {rel}::{name} — omitted, context budget exhausted")
            continue
        sections.append(section)
        budget -= len(section)
    return "\n\n".join(sections)


def _full(diff: str, root: Path) -> str:
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
    return "\n\n".join(sections)


def gather_changed_context(diff: str, root: str | Path = ".", mode: str | None = None) -> str:
    """The ``<changed_code_context>`` block for ``diff``, or ``""`` when nothing fits.

    ``mode`` (default: ``$BAIR_CONTEXT``, else ``slice``): ``slice`` sends function
    bodies along the data flow; ``full`` sends whole touched files plus call sites;
    ``none`` sends nothing. The default moves only on the eval suite's held-out
    numbers: on 2026-09-26 (server-bot, 6+6 held-out, 3 runs) slice matched full on
    BLOCKs (0/6 vs 0/6) and false BLOCKs (0/6 vs 0/6), flagged 4/6 vs 3/6 at ≥WARN,
    with ~3x less context — non-inferior and cheaper, so it became the default."""
    mode = (mode or os.environ.get(_MODE_ENV) or "slice").lower()
    if mode not in _MODES:
        raise ValueError(f"unknown {_MODE_ENV}={mode!r}; expected one of {_MODES}")
    root = Path(root)
    if mode == "none" or not diff or not root.is_dir():
        return ""
    body = _slice(diff, root) if mode == "slice" else _full(diff, root)
    if not body:
        return ""
    return "<changed_code_context>\n" + body + "\n</changed_code_context>"
