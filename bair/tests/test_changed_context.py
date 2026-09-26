"""Tests for the changed_context gatherer.

Por qué existe (2026-09-25, backlog 01): en server-bot#104 el defecto tumbaba
``_expired`` al reanudar, en el mismo archivo pero fuera del hunk, y BAIR no lo
vio. El gatherer le pasa el archivo completo y los sitios que llaman a lo que
cambió. Repos git temporales, sin red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bair.gatherers import changed_context as cc
from bair.pipelines.gatekeep import _build_user_msg

DIFF = """\
diff --git a/pkg/jobs.py b/pkg/jobs.py
--- a/pkg/jobs.py
+++ b/pkg/jobs.py
@@ -3,2 +3,2 @@ def _is_ref(block):
-    if all(is_ref(b) for b in blocks):
+    if any(is_ref(b) for b in blocks):
diff --git a/tests/test_jobs.py b/tests/test_jobs.py
--- a/tests/test_jobs.py
+++ b/tests/test_jobs.py
@@ -1,1 +1,2 @@
+def test_new(): pass
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
"""


def _repo(tmp_path: Path) -> Path:
    files = {
        "pkg/jobs.py": "def _payload(req):\n    blocks = req\n    if any(is_ref(b) for b in blocks):\n        pass\n\n"
        "def _expired(blocks):\n    return blocks[0]['source']['url']\n",
        "pkg/runner.py": "from pkg.jobs import _payload\n\nrow = _payload(req)\n",
        "tests/test_jobs.py": "def test_new(): pass\n",
    }
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_parses_changed_files():
    assert cc.changed_files(DIFF) == ["pkg/jobs.py", "tests/test_jobs.py"]


def test_the_changed_function_is_the_one_enclosing_the_line_not_the_hunk_header(tmp_path):
    # git names the def BEFORE the hunk (`_is_ref` here); the line lives in `_payload`.
    assert cc.changed_functions(DIFF, _repo(tmp_path)) == ["_payload"]


def test_a_deletion_only_hunk_still_names_its_function(tmp_path):
    diff = "+++ b/pkg/jobs.py\n@@ -6,3 +6,2 @@\n def _expired(blocks):\n-    x = 1\n     return 1\n"
    assert cc.changed_functions(diff, _repo(tmp_path)) == ["_expired"]


def test_the_code_outside_the_hunk_reaches_the_review(tmp_path):
    block = cc.gather_changed_context(DIFF, _repo(tmp_path), mode="full")
    assert block.startswith("<changed_code_context>")
    assert "FILE: pkg/jobs.py (full text after the change)" in block
    assert "def _expired(blocks):" in block
    assert "CALLERS OF _payload" in block and "pkg/runner.py" in block
    assert block.index("FILE: pkg/jobs.py") < block.index("FILE: tests/test_jobs.py")


def test_budget_omits_instead_of_overflowing(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_MAX_BYTES", 150)
    block = cc.gather_changed_context(DIFF, _repo(tmp_path), mode="full")
    assert "context budget exhausted" in block
    assert len(block) < 600


def test_nothing_readable_is_an_empty_block(tmp_path):
    assert cc.gather_changed_context(DIFF, tmp_path) == ""
    assert cc.gather_changed_context("", tmp_path) == ""
    assert cc.gather_changed_context(DIFF, tmp_path / "missing") == ""


def test_the_block_sits_right_before_the_diff():
    msg = _build_user_msg("THE-DIFF", "", "o/r", "1", code_context="<changed_code_context>X</changed_code_context>")
    assert msg.index("<changed_code_context>") < msg.index("DIFF:\n\nTHE-DIFF")
    assert "Changed-code context" not in _build_user_msg("THE-DIFF", "", "o/r", "1")


# -- slice mode: function bodies along the data flow, not whole files --------------

SLICE_DIFF = """\
diff --git a/pkg/jobs.py b/pkg/jobs.py
--- a/pkg/jobs.py
+++ b/pkg/jobs.py
@@ -1,6 +1,6 @@
 def _payload(req):
     data = {}
-    if all(req):
+    if any(req):
         data["attachments"] = req
     return data
"""


def _slice_repo(tmp_path: Path) -> Path:
    files = {
        "pkg/jobs.py": (
            "def _payload(req):\n    data = {}\n    if any(req):\n        data[\"attachments\"] = req\n    return data\n\n\n"
            "def _expired(blocks):\n    return blocks[0]['source']['url']\n\n\n"
            "def resume(row):\n    return _expired(row.get(\"attachments\"))\n\n\n"
            "def unrelated():\n    return 42\n"
        ),
        "pkg/runner.py": "from pkg.jobs import _payload\n\n\ndef submit(req):\n    return _payload(req)\n",
        "tests/test_other.py": "def _payload():\n    return {}\n",
    }
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_slice_follows_the_data_to_its_readers(tmp_path):
    block = cc.gather_changed_context(SLICE_DIFF, _slice_repo(tmp_path), mode="slice")
    assert "FUNCTION pkg/jobs.py::_payload" in block
    assert "FUNCTION pkg/jobs.py::resume" in block and "touches 'attachments'" in block
    assert "FUNCTION pkg/jobs.py::_expired" in block and "called by resume" in block
    assert "FUNCTION pkg/runner.py::submit" in block
    assert "unrelated" not in block
    assert "tests/test_other.py" not in block  # a same-named def in a test is not a caller


def test_modes_are_selectable_and_validated(tmp_path, monkeypatch):
    root = _slice_repo(tmp_path)
    assert cc.gather_changed_context(SLICE_DIFF, root, mode="none") == ""
    monkeypatch.setenv("BAIR_CONTEXT", "slice")
    assert "FUNCTION " in cc.gather_changed_context(SLICE_DIFF, root)
    monkeypatch.delenv("BAIR_CONTEXT")
    assert "FUNCTION pkg/jobs.py::_payload" in cc.gather_changed_context(SLICE_DIFF, root)  # slice is the default
    assert "FILE: pkg/jobs.py" in cc.gather_changed_context(SLICE_DIFF, root, mode="full")
    import pytest

    with pytest.raises(ValueError):
        cc.gather_changed_context(SLICE_DIFF, root, mode="everything")
