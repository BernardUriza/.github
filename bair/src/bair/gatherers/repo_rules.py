"""repo_rules gatherer — read the doctrine the gatekeeper reviews a diff against,
across BOTH layers:

  - the TARGET repo's OWN rules (``.claude/CLAUDE.md`` + ``.claude/rules/**/*.md``),
    so a finding can cite the project's local doctrine; and
  - the UNIVERSAL engineering playbook (the curated always-on subset of
    ``BernardUriza/engineering-playbook``) that applies to EVERY repo — the
    Constitution, prompts-as-content, no-code-comments, secrets, git law, etc.

Most repos carry only 1-2 local rules, so without the universal layer the
gatekeeper cannot enforce cross-repo law. Both layers are emitted as separate
provenance-tagged blocks: ``<repository_rules>`` (provenance ``FILE: <relpath>``)
and ``<universal_rules>`` (provenance ``FILE: playbook/<name>``). Provenance is
mandatory: without it the model cannot CITE which rule a finding violates, and an
uncited "doctrine violation" is exactly the generic criticism this fixes.

CI sourcing of the universal layer: the runner has no ``~/.claude/`` home dir, so
the gatekeep workflow checks ``BernardUriza/engineering-playbook`` out into a known
path and points :data:`_PLAYBOOK_DIR_ENV` at its ``rules/`` directory. The dir is
resolved from that env var first, then a few sane default checkout paths — never a
machine-specific absolute path. The curated subset is an allow-list of filenames
(:data:`_CURATED_PLAYBOOK_RULES`) so the injected block stays bounded even as the
playbook grows past 39 rules.

Fail-soft everywhere: a repo with no ``.claude/`` and/or no reachable playbook dir
returns ``""`` for that layer and the gatekeeper falls back — never an error. Pure
stdlib (no xair) so it is unit-testable in isolation.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

_PRIORITY_STEMS = (
    "constitution",
    "00-",
    "playbook",
    "framework",
    "prompts-as-content",
    "no-code-comments",
    "test",
    "security",
    "secret",
    "git",
)

_DEFAULT_MAX_BYTES = 60_000
_PLAYBOOK_MAX_BYTES = 100_000

_CURATED_PLAYBOOK_RULES = (
    "00-constitution.md",
    "agent-autonomy.md",
    "artifact-delivery.md",
    "audience-aware-communication.md",
    "backlog-handling.md",
    "chrome-devtools-contenteditable-input.md",
    "coagent.md",
    "git.md",
    "no-code-comments.md",
    "prompts-as-content-not-code.md",
    "secrets-management.md",
    "verify-before-assuming.md",
)

_PLAYBOOK_DIR_ENV = "BAIR_PLAYBOOK_RULES_DIR"

_PLAYBOOK_DEFAULT_DIRS = (
    "_bair_playbook/rules",
    "_playbook/rules",
    "engineering-playbook/rules",
)


def _priority(path: Path) -> int:
    """Lower sorts first. CLAUDE.md leads; then priority stems; then alphabetical."""
    name = path.name.lower()
    if name == "claude.md":
        return -1
    for i, stem in enumerate(_PRIORITY_STEMS):
        if stem in name:
            return i
    return len(_PRIORITY_STEMS)


def _assemble_block(tag: str, entries: list[tuple[str, str]], max_bytes: int, truncation_note: str) -> str:
    """Render ``(provenance_label, content)`` entries into one ``<tag>`` block.

    Each entry becomes ``FILE: <label>`` + its trimmed content so the model can
    cite by path. Truncates at ``max_bytes`` (entries are pre-sorted by priority)
    with an explicit marker. Returns ``""`` when nothing fits."""
    blocks: list[str] = []
    used = 0
    truncated = False
    for label, content in entries:
        block = f"FILE: {label}\n{content.strip()}\n"
        if used + len(block) > max_bytes:
            remaining = max_bytes - used
            if remaining > 200:  # room for a meaningful partial file
                blocks.append(block[:remaining])
            truncated = True
            break
        blocks.append(block)
        used += len(block)

    if not blocks:
        return ""

    body = "\n".join(blocks)
    if truncated:
        body += f"\n{truncation_note}"
    return f"<{tag}>\n{body}\n</{tag}>"


def _read_entries(files: list[Path], label_for: Callable[[Path], str]) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        entries.append((label_for(path), content))
    return entries


def _collect_files(root: Path) -> list[Path]:
    claude_dir = root / ".claude"
    if not claude_dir.is_dir():
        return []
    files: list[Path] = []
    claude_md = claude_dir / "CLAUDE.md"
    if claude_md.is_file():
        files.append(claude_md)
    rules_dir = claude_dir / "rules"
    if rules_dir.is_dir():
        files.extend(sorted(rules_dir.rglob("*.md"), key=lambda p: str(p)))
    return sorted(files, key=lambda p: (_priority(p), str(p)))


def gather_repo_rules(root: str | Path = ".", max_bytes: int = _DEFAULT_MAX_BYTES) -> str:
    """Return the TARGET repo's rules as one ``<repository_rules>`` block, or ``""``
    when the repo ships none. Each file is prefixed ``FILE: <relpath>`` for
    citation. Truncates at ``max_bytes`` (priority files first) with a marker."""
    root = Path(root)
    files = _collect_files(root)
    if not files:
        return ""

    def _label(path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    entries = _read_entries(files, _label)
    return _assemble_block(
        "repository_rules",
        entries,
        max_bytes,
        "[repository rules truncated — lower-priority rule files omitted]",
    )


def resolve_playbook_dir(explicit: str | Path | None = None) -> Path | None:
    """Resolve the playbook ``rules/`` directory: explicit arg, then the
    :data:`_PLAYBOOK_DIR_ENV` env var, then the sane default checkout paths.
    Returns ``None`` when none resolves to a real directory (fail-soft)."""
    if explicit is not None:
        p = Path(explicit)
        return p if p.is_dir() else None
    env = os.environ.get(_PLAYBOOK_DIR_ENV, "").strip()
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    for cand in _PLAYBOOK_DEFAULT_DIRS:
        p = Path(cand)
        if p.is_dir():
            return p
    return None


def gather_playbook_rules(
    playbook_dir: str | Path | None = None, max_bytes: int = _PLAYBOOK_MAX_BYTES
) -> str:
    """Return the curated universal playbook rules as one ``<universal_rules>``
    block, or ``""`` when the playbook dir is unreachable (e.g. a runner that did
    not check it out). Only the :data:`_CURATED_PLAYBOOK_RULES` allow-list is read;
    each is prefixed ``FILE: playbook/<name>`` so a finding cites the universal
    rule it violates, distinctly from the repo-local ``<repository_rules>`` block."""
    rules_dir = resolve_playbook_dir(playbook_dir)
    if rules_dir is None:
        return ""

    files = [rules_dir / name for name in _CURATED_PLAYBOOK_RULES]
    files = [p for p in files if p.is_file()]
    if not files:
        return ""
    files.sort(key=lambda p: (_priority(p), str(p)))

    entries = _read_entries(files, lambda p: f"playbook/{p.name}")
    return _assemble_block(
        "universal_rules",
        entries,
        max_bytes,
        "[universal rules truncated — lower-priority playbook files omitted]",
    )
