"""Changelog pipeline — gather commits -> format -> LLM -> deliver."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import ChangelogConfig
from ..domain.models import ChangelogOutput
from ..infra.constants import CHANGELOG_OUTPUT, DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
from ..infra.container import Container
from ..infra.slack import post_to_slack
from ..gatherers.commits import gather_commits
from ..prompt.changelog_formatter import format_commit_input
from ..services.changelog.deliverers import write_changelog_file


def run_changelog(container: Container, cfg: ChangelogConfig | None = None) -> None:
    """Execute the full changelog pipeline."""
    c = container
    if cfg is None:
        cfg = ChangelogConfig.from_env()

    c.actions.notice(f"Generating changelog: {cfg.base_ref}..{cfg.head_ref} for {cfg.repo}")

    commits = gather_commits(c.github, cfg)
    if not commits:
        c.actions.warning(f"No commits found between {cfg.base_ref} and {cfg.head_ref}")
        return
    c.actions.notice(f"Found {len(commits)} commits")

    system_prompt = Path(cfg.prompt_file).read_text(encoding="utf-8")
    user_message = format_commit_input(commits, cfg)
    changelog_data = c.llm.call(
        system=system_prompt, user=user_message,
        model=cfg.model, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE,
    )
    output = ChangelogOutput.from_dict(changelog_data)

    if cfg.slack_webhook:
        try:
            post_to_slack(cfg.slack_webhook, output.slack_message, actions=c.actions)
        except Exception as exc:
            c.actions.warning(f"Slack post failed: {exc}")
    else:
        c.actions.notice("Slack webhook not configured -- skipping")

    write_changelog_file(output.detailed_markdown)
    c.store.write(CHANGELOG_OUTPUT, json.dumps(changelog_data, indent=2))
    c.actions.notice(f"Changelog complete: {len(commits)} commits processed")
