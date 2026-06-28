"""repo_rules gatherer — read the TARGET repo's own doctrine so the gatekeeper
reviews a diff against the project's rules, not just generic code smell.

The gatekeep workflow checks the PR head out at the repo root (``fetch-depth: 0``),
so this reads ``.claude/CLAUDE.md`` + ``.claude/rules/**/*.md`` from the working
tree and returns one provenance-tagged block. Provenance (``FILE: <path>``) is
mandatory: without it the model cannot CITE which rule a finding violates, and an
uncited "doctrine violation" is exactly the generic criticism this fixes.

Fail-soft: a repo with no ``.claude/`` returns ``""`` and the gatekeeper falls back
to a generic review — never an error. Pure stdlib (no xair) so it is unit-testable
in isolation.
"""

from __future__ import annotations

from pathlib import Path

# When the gathered rules exceed the cap, keep the files whose names match these
# stems FIRST — the load-bearing doctrine (the charter, the universal rules, the
# P0 content/test/security rules) must survive truncation; a long stack-specific
# rule is the first to drop.
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


def _priority(path: Path) -> int:
    """Lower sorts first. CLAUDE.md leads; then priority stems; then alphabetical."""
    name = path.name.lower()
    if name == "claude.md":
        return -1
    for i, stem in enumerate(_PRIORITY_STEMS):
        if stem in name:
            return i
    return len(_PRIORITY_STEMS)


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
    # Stable order: priority bucket, then deterministic alphabetical path.
    return sorted(files, key=lambda p: (_priority(p), str(p)))


def gather_repo_rules(root: str | Path = ".", max_bytes: int = _DEFAULT_MAX_BYTES) -> str:
    """Return the repo's rules as one ``<repository_rules>`` block, or ``""`` when
    the repo ships none. Each file is prefixed ``FILE: <relpath>`` for citation.
    Truncates at ``max_bytes`` (priority files first) with an explicit marker."""
    root = Path(root)
    files = _collect_files(root)
    if not files:
        return ""

    blocks: list[str] = []
    used = 0
    truncated = False
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        block = f"FILE: {rel}\n{content.strip()}\n"
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
        body += "\n[repository rules truncated — lower-priority rule files omitted]"
    return f"<repository_rules>\n{body}\n</repository_rules>"
