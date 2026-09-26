"""The reviewer's read-only tools over one pinned review (a capsule).

Every tool reads the mirror at the capsule's commits — the model picks paths and
patterns, never the repository or the commit. Arguments are model output, so
each one is validated before it reaches ``git``: paths go after ``--``, patterns
after ``-e``, and nothing may start with ``-``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import mirror

MAX_CHARS = 60_000
MAX_READ_LINES = 1_500
MAX_BLAME_LINES = 400


@dataclass(frozen=True)
class Capsule:
    repo: str
    base_sha: str
    head_sha: str


class ToolError(Exception):
    """Bad arguments from the model — returned as a tool error, never raised out."""


def _rev(cap: Capsule, args: dict[str, Any]) -> str:
    rev = args.get("rev", "head")
    if rev not in ("head", "base"):
        raise ToolError("rev must be 'head' (the PR) or 'base' (before the PR)")
    return cap.head_sha if rev == "head" else cap.base_sha


def _path(args: dict[str, Any], *, required: bool) -> str:
    raw = args.get("path", "")
    if raw in (None, ""):
        if required:
            raise ToolError("path is required")
        return ""
    if not isinstance(raw, str) or len(raw) > 1024 or "\0" in raw:
        raise ToolError("path must be a plain string")
    path = raw.strip().lstrip("/").removeprefix("./")
    if path.startswith("-") or path.startswith(":"):
        raise ToolError("path may not start with '-' or ':'")
    return path.rstrip("/")


def _int(args: dict[str, Any], key: str, default: int | None, lo: int = 1, hi: int = 1_000_000) -> int | None:
    value = args.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ToolError(f"{key} must be an integer in [{lo}, {hi}]")
    return value


def _note(result: mirror.GitResult, what: str) -> str:
    return result.out + (f"\n[truncated: {what} exceeded the {MAX_CHARS}-char cap — narrow the request]"
                         if result.truncated else "")


def list_files(cap: Capsule, args: dict[str, Any]) -> str:
    path = _path(args, required=False)
    recursive = bool(args.get("recursive", False))
    cmd = ["ls-tree", "--name-only"] + (["-r"] if recursive else []) + [_rev(cap, args), "--"]
    cmd += [f"{path}/"] if path else []
    return _note(mirror.read(cap.repo, cmd, max_chars=MAX_CHARS), "the listing") or "(empty)"


def read_file(cap: Capsule, args: dict[str, Any]) -> str:
    path = _path(args, required=True)
    start = _int(args, "start_line", 1) or 1
    end = _int(args, "end_line", None)
    if end is not None and end < start:
        raise ToolError("end_line must be >= start_line")
    kind = mirror.read(cap.repo, ["cat-file", "-t", f"{_rev(cap, args)}:{path}"], max_chars=64).out.strip()
    if kind == "tree":
        raise ToolError(f"{path} is a directory — use list_files")
    if kind != "blob":
        raise ToolError(f"{path} does not exist at {args.get('rev', 'head')}")
    text = mirror.read(cap.repo, ["cat-file", "-p", f"{_rev(cap, args)}:{path}"], max_chars=5_000_000).out
    lines = text.splitlines()
    stop = min(len(lines), end if end is not None else start - 1 + MAX_READ_LINES, start - 1 + MAX_READ_LINES)
    body = "\n".join(f"{n:>6}\t{lines[n - 1]}" for n in range(start, stop + 1))
    tail = f"\n[lines {start}-{stop} of {len(lines)}]" if stop < len(lines) or start > 1 else ""
    return (body[:MAX_CHARS] or "(empty file)") + tail


def grep(cap: Capsule, args: dict[str, Any]) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 500 or "\0" in pattern:
        raise ToolError("pattern must be a non-empty string (extended regex)")
    cmd = ["grep", "-n", "-I", "-E", "--full-name"]
    if args.get("ignore_case"):
        cmd.append("-i")
    cmd += ["-e", pattern, _rev(cap, args), "--"]
    glob = args.get("path_glob")
    if glob:
        if not isinstance(glob, str) or len(glob) > 300 or glob.startswith("-") or "\0" in glob:
            raise ToolError("path_glob must be a plain glob like 'src/**/*.py'")
        cmd.append(f":(glob){glob}")
    result = mirror.read(cap.repo, cmd, max_chars=MAX_CHARS)
    prefix = _rev(cap, args) + ":"
    out = "\n".join(line.removeprefix(prefix) for line in result.out.splitlines())
    return _note(mirror.GitResult(out, result.truncated), "the matches") or "(no matches)"


def git_log(cap: Capsule, args: dict[str, Any]) -> str:
    path = _path(args, required=False)
    limit = _int(args, "limit", 20, hi=200)
    cmd = ["log", f"-n{limit}", "--date=short", "--format=%h %ad %an  %s", cap.head_sha, "--"]
    cmd += [path] if path else []
    return _note(mirror.read(cap.repo, cmd, max_chars=MAX_CHARS), "the log") or "(no commits)"


def blame(cap: Capsule, args: dict[str, Any]) -> str:
    path = _path(args, required=True)
    start = _int(args, "start_line", 1) or 1
    end = _int(args, "end_line", start + 50) or start + 50
    if end < start or end - start >= MAX_BLAME_LINES:
        raise ToolError(f"blame at most {MAX_BLAME_LINES} lines, end_line >= start_line")
    cmd = ["blame", "--date=short", "-L", f"{start},{end}", _rev(cap, args), "--", path]
    return _note(mirror.read(cap.repo, cmd, max_chars=MAX_CHARS), "the blame")


def diff(cap: Capsule, args: dict[str, Any]) -> str:
    path = _path(args, required=False)
    cmd = ["diff", f"{cap.base_sha}...{cap.head_sha}", "--"] + ([path] if path else [])
    return _note(mirror.read(cap.repo, cmd, max_chars=MAX_CHARS), "the diff") or "(no changes)"


_REV = {"type": "string", "enum": ["head", "base"],
        "description": "'head' = the PR's version (default), 'base' = the code before the PR"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    handler: Callable[[Capsule, dict[str, Any]], str]

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": {"type": "object", "properties": self.properties,
                                "required": list(self.required), "additionalProperties": False}}


TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("list_files", "List files of the repository under the review at a directory (read-only).",
         {"path": {"type": "string", "description": "directory; empty = repo root"},
          "recursive": {"type": "boolean"}, "rev": _REV}, (), list_files),
    Tool("read_file", "Read a file of the repository under review, with line numbers (read-only).",
         {"path": {"type": "string"}, "start_line": {"type": "integer"},
          "end_line": {"type": "integer"}, "rev": _REV}, ("path",), read_file),
    Tool("grep", "Search the whole repository under review with an extended regex (git grep).",
         {"pattern": {"type": "string"}, "path_glob": {"type": "string", "description": "e.g. 'src/**/*.py'"},
          "ignore_case": {"type": "boolean"}, "rev": _REV}, ("pattern",), grep),
    Tool("git_log", "History of the repository (or of one path) up to the PR head.",
         {"path": {"type": "string"}, "limit": {"type": "integer"}}, (), git_log),
    Tool("blame", "Who last changed each line of a file range, and when.",
         {"path": {"type": "string"}, "start_line": {"type": "integer"},
          "end_line": {"type": "integer"}, "rev": _REV}, ("path",), blame),
    Tool("diff", "The PR's diff (base...head), optionally for one path.",
         {"path": {"type": "string"}}, (), diff),
)}


def call(cap: Capsule, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run one tool; returns ``(text, is_error)`` and never raises."""
    tool = TOOLS.get(name)
    if tool is None:
        return f"unknown tool {name!r}", True
    try:
        return tool.handler(cap, dict(args or {})), False
    except (ToolError, mirror.MirrorError) as exc:
        return str(exc), True
