"""Deep analysis gatherer -- invokes Claude as a read-only code analysis assistant.

Claude reads the repo, traces data flows, checks callers, and returns
structured JSON observations. These observations become a prompt tier
for the GPT reviewer -- they are NOT posted to GitHub directly.

Now uses the LlmProvider protocol (via container.get_deep_llm()) instead
of raw HTTP calls. This gives retry, token logging, and testability for free.
"""

from __future__ import annotations

import json
from functools import lru_cache

from ..domain.models import PRIdentifier
from ..infra.constants import TEMPLATES_DIR
from ..contracts import ActionsIO, FileStore, LlmProvider
from ..log import logger

_PROMPT_DIR = TEMPLATES_DIR.parent  # prompts/
_DEEP_ANALYSIS_STORE_KEY = "deep-analysis.json"
_DEEP_ANALYSIS_MODEL = "claude-sonnet-4-6"
_DEEP_MAX_TOKENS = 4096
_DEEP_TEMPERATURE = 0.1
_MAX_DIFF_CHARS = 60_000


@lru_cache(maxsize=1)
def _load_prompt() -> str:
    path = _PROMPT_DIR / "deep-analysis.md"
    if not path.exists():
        raise FileNotFoundError(f"Deep analysis prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def _format_observations(result: dict) -> str:
    """Format Claude's observations as text for the GPT prompt tier."""
    observations = result.get("observations", [])
    if not observations:
        return ""

    lines = []
    for i, obs in enumerate(observations, 1):
        type_tag = obs.get("type", "?")
        file = obs.get("file", "?")
        line = obs.get("line", "?")
        text = obs.get("observation", "")
        evidence = obs.get("evidence", "")
        confidence = obs.get("confidence", "?")
        related = obs.get("related_files", [])

        lines.append(f"{i}. [{type_tag}] `{file}:{line}` \u2014 {text}")
        if evidence:
            lines.append(f"   Evidence: {evidence}")
        if related:
            lines.append(f"   Related: {', '.join(related)}")
        lines.append(f"   Confidence: {confidence}")
        lines.append("")

    return "\n".join(lines)


def gather_deep_analysis(
    pr: PRIdentifier,
    diff: str,
    deep_llm: LlmProvider,
    store: FileStore,
    actions: ActionsIO,
) -> str:
    """Run Claude deep analysis on the PR via the LlmProvider protocol.

    Args:
        deep_llm: The LLM provider for deep analysis (AgentSDKProvider —
                  Claude Code with repo tools, via container.get_deep_llm()).

    Returns formatted text for the GPT prompt tier, or empty string on failure.
    """
    actions.notice("Deep analysis: invoking Claude for codebase inspection")

    system_prompt = _load_prompt()

    changed_files = [line[6:] for line in diff.splitlines() if line.startswith("+++ b/")]
    user_message = (
        f"## Changed files\n{chr(10).join(changed_files)}"
        f"\n\n## PR Diff\n```\n{diff[:_MAX_DIFF_CHARS]}\n```"
    )

    result = deep_llm.call(
        system=system_prompt,
        user=user_message,
        model=_DEEP_ANALYSIS_MODEL,
        max_tokens=_DEEP_MAX_TOKENS,
        temperature=_DEEP_TEMPERATURE,
    )

    logger.debug(f"  [deep_analysis] llm.call returned keys: {list(result.keys())}")
    logger.debug(f"  [deep_analysis] _raw present: {'_raw' in result}")

    store.write(_DEEP_ANALYSIS_STORE_KEY, json.dumps(result, indent=2))

    observations = result.get("observations", [])
    summary = result.get("summary", "")
    actions.notice(f"Deep analysis: {len(observations)} observations. {summary}")

    formatted = _format_observations(result)
    logger.debug(f"  [deep_analysis] _format_observations returned: {len(formatted)} chars")
    if formatted:
        logger.debug(f"  [deep_analysis] first 200 chars: {formatted[:200]}")
    else:
        logger.debug(f"  [deep_analysis] WARNING: formatted is EMPTY despite {len(observations)} observations")

    return formatted
