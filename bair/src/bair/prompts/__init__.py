"""Runtime loader for bair's model-facing prompts.

Model-facing prompts (system prompts, personas, classifier instructions) live
as content files (``*.md``) in this package, shipped with the wheel, instead of
as inline string constants in source. Editing a prompt is then an edit to a
content file, not a code change + redeploy.

The loader is mtime-aware: it caches a prompt's text keyed by the file's
modification time and re-reads the file when that mtime changes. A live edit to
the ``.md`` is therefore picked up on the next ``load_prompt`` call without
restarting the process.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent

_cache: dict[str, tuple[float, str]] = {}


def prompts_dir() -> Path:
    return _PROMPTS_DIR


def available_prompts() -> list[str]:
    return sorted(p.stem for p in _PROMPTS_DIR.glob("*.md"))


def load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Prompt content file not found: {path}. "
            f"Available prompts: {available_prompts()}"
        ) from exc

    cached = _cache.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    text = path.read_text(encoding="utf-8")
    _cache[name] = (mtime, text)
    return text
