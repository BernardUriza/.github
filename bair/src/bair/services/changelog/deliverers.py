"""Delivery mechanisms for changelog output — file writer and Slack poster."""

from __future__ import annotations

from pathlib import Path


def write_changelog_file(markdown: str) -> None:
    """Prepend new changelog entry to CHANGELOG.md."""
    changelog_file = Path("CHANGELOG.md")
    if changelog_file.exists():
        existing = changelog_file.read_text(encoding="utf-8")
        changelog_file.write_text(markdown + "\n\n" + existing, encoding="utf-8")
    else:
        changelog_file.write_text(
            f"# Changelog\n\n> Auto-generated changelog.\n\n{markdown}\n",
            encoding="utf-8",
        )
