"""Tests for the model-facing prompt loader (bair.prompts).

Pure stdlib — no xair needed, so these run in local dev and CI alike.
"""

from __future__ import annotations

import time

import pytest

from bair import prompts
from bair.prompts import available_prompts, load_prompt, prompts_dir

_EXPECTED_PROMPTS = {"gatekeep_system"}


def test_every_shipped_prompt_resolves():
    names = set(available_prompts())
    assert _EXPECTED_PROMPTS <= names, f"missing prompts: {_EXPECTED_PROMPTS - names}"
    for name in names:
        text = load_prompt(name)
        assert text.strip(), f"prompt {name!r} loaded empty"


def test_gatekeep_system_prompt_content():
    text = load_prompt("gatekeep_system")
    assert text.startswith("You are a code review gatekeeper.")
    assert "APPROVE" in text and "WARN" in text and "BLOCK" in text
    assert "playbook/prompts-as-content-not-code.md" in text


def test_missing_prompt_raises_filenotfound():
    with pytest.raises(FileNotFoundError) as exc:
        load_prompt("does_not_exist_xyz")
    assert "does_not_exist_xyz" in str(exc.value)
    assert "Available prompts" in str(exc.value)


def test_hot_reload_picks_up_edits(tmp_path, monkeypatch):
    md = tmp_path / "ephemeral.md"
    md.write_text("first version", encoding="utf-8")
    monkeypatch.setattr(prompts, "_PROMPTS_DIR", tmp_path)
    prompts._cache.clear()

    assert load_prompt("ephemeral") == "first version"

    time.sleep(0.01)
    md.write_text("second version", encoding="utf-8")
    # bump mtime explicitly so the change is observable regardless of fs granularity
    future = md.stat().st_mtime + 5
    import os

    os.utime(md, (future, future))

    assert load_prompt("ephemeral") == "second version"


def test_prompts_dir_is_inside_package():
    assert prompts_dir().name == "prompts"
    assert prompts_dir().is_dir()
