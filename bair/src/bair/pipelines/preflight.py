"""Preflight pipeline (<ticket-id>) — pre-deploy announcement.

Gathers commits queued for the next deploy, sends them through the LLM with
the preflight prompt (dual-audience output: customer impact one-liner +
engineer summary + risk flags), and posts a Slack Block Kit card to a
release-coordination channel.

Sibling to pipelines/changelog.py — same commit-gathering primitives,
different prompt, different Slack delivery format. Where changelog is the
"news article after the thing happened" (Jason's framing), preflight is
the "warning signal before the thing happens".

Trigger: workflow_dispatch from .github/workflows/preflight.yml with
inputs target-time, target-service.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import PreflightConfig
from ..domain.models import PreflightOutput
from ..infra.constants import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
from ..infra.container import Container
from ..infra.slack import post_blocks_to_slack
from ..gatherers.commits import gather_commits
from ..prompt.preflight_formatter import format_preflight_input


# Same artifact-key convention as CHANGELOG_OUTPUT — keeps the GitHub Actions
# step output readable for debugging.
PREFLIGHT_OUTPUT = "preflight-output.json"


def run_preflight(container: Container, cfg: PreflightConfig | None = None) -> None:
    """Execute the full preflight pipeline."""
    c = container
    if cfg is None:
        cfg = PreflightConfig.from_env()

    c.actions.notice(
        f"Preflight: {cfg.base_ref}..{cfg.head_ref} for {cfg.repo} "
        f"(service={cfg.target_service}, target_time={cfg.target_time or '<unspecified>'})"
    )

    # Reuse gather_commits — same --first-parent + PR# extraction. Adapter
    # avoids constructing a fake ChangelogConfig by passing fields directly.
    commits = _gather_via_changelog_shape(c, cfg)
    if not commits:
        c.actions.warning(
            f"No commits queued between {cfg.base_ref} and {cfg.head_ref}. "
            "Nothing to announce — skipping Slack post."
        )
        return
    c.actions.notice(f"Found {len(commits)} commits queued for next deploy")

    system_prompt = Path(cfg.prompt_file).read_text(encoding="utf-8")
    user_message = format_preflight_input(commits, cfg)
    raw = c.llm.call(
        system=system_prompt, user=user_message,
        model=cfg.model, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE,
    )
    output = PreflightOutput.from_dict(raw)
    c.actions.notice(
        f"LLM output: urgency={output.release_urgency} "
        f"impact='{output.customer_impact_oneliner[:80]}...' "
        f"risk_flags={output.risk_flags}"
    )

    blocks = _build_blocks(output, cfg, commit_count=len(commits))
    fallback = (
        f"Upcoming release ({cfg.target_service}, {output.release_urgency} urgency) "
        f"at {cfg.target_time or 'TBD'}: {output.customer_impact_oneliner}"
    )

    if cfg.slack_webhook:
        try:
            post_blocks_to_slack(
                cfg.slack_webhook, blocks,
                fallback_text=fallback, actions=c.actions,
            )
        except Exception as exc:  # noqa: BLE001 — webhook errors are non-fatal
            c.actions.warning(f"Slack post failed: {exc}")
    else:
        c.actions.notice("Slack webhook not configured -- skipping post")

    c.store.write(PREFLIGHT_OUTPUT, json.dumps({
        "customer_impact_oneliner": output.customer_impact_oneliner,
        "engineer_summary": output.engineer_summary,
        "risk_flags": output.risk_flags,
        "release_urgency": output.release_urgency,
        "commit_count": len(commits),
        "target_service": cfg.target_service,
        "target_time": cfg.target_time,
    }, indent=2))
    c.actions.notice(f"Preflight complete: {len(commits)} commits, {len(output.risk_flags)} risk flags")


# ── internal helpers ────────────────────────────────────────────────────


def _gather_via_changelog_shape(c: Container, cfg: PreflightConfig):
    """Bridge between PreflightConfig and gather_commits (which takes
    ChangelogConfig). gather_commits only reads base_ref/head_ref/since_date/
    until_date, so we synthesize a minimal ChangelogConfig-shaped object."""
    from ..config import ChangelogConfig

    bridge = ChangelogConfig(
        BASE_REF=cfg.base_ref,
        HEAD_REF=cfg.head_ref,
        REPO=cfg.repo,
    )  # type: ignore[call-arg]  # pydantic-settings pattern, same as ChangelogConfig.from_env
    return gather_commits(c.github, bridge)


_URGENCY_CHIPS = {
    "high":   "🔴 high urgency",
    "medium": "🟡 medium urgency",
    "low":    "🟢 low urgency",
}


def _build_blocks(output: PreflightOutput, cfg: PreflightConfig, *, commit_count: int) -> list[dict]:
    """Compose the Slack Block Kit payload.

    Layout (top to bottom):
      1. Header: 📅 Upcoming release · <urgency chip>
      2. Fields: When | Service
      3. Customer impact (italic, the warning-signal punch)
      4. Engineer summary
      5. Risk flags (only if any — section is skipped when empty)
      6. Context: PR count + ack instructions
    """
    when_text = cfg.target_time or "_(time TBD)_"
    urgency_chip = _URGENCY_CHIPS.get(output.release_urgency, _URGENCY_CHIPS["medium"])
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📅 Upcoming release · {urgency_chip}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*When:*\n{when_text}"},
                {"type": "mrkdwn", "text": f"*Service:*\n`{cfg.target_service}`"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Customer impact:*\n_{output.customer_impact_oneliner}_",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Changes ({commit_count} PRs):*\n{output.engineer_summary}"},
        },
    ]

    if output.risk_flags:
        flag_lines = "\n".join(f"• `{f}`" for f in output.risk_flags)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⚠️ *Risk flags:*\n{flag_lines}"},
        })

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "React 👀 to acknowledge · ✅ to go · 🚫 to block (DM Bernard with reason)",
        }],
    })
    return blocks
