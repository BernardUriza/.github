"""bair's repo MCP — the door AIRE's reviewer reads the repository through.

AIRE never touches git (aire-server CLAUDE.md, decision #3): git enters as a
tool configured from outside, at the caller's layer, wired through the door's
``remote_tools`` (#48). Same shape as persona-runner's ``/mcp/{casita}``:

- ``POST /prepare`` — called by bair FROM THE RUNNER with the workflow's own
  GitHub token. Proves read access, fetches both commits into the permanent
  mirror and mints a capsule pinned to (repo, base, head). The GitHub token
  never reaches AIRE.
- ``POST /mcp/{capsule}`` — stateless JSON-RPC (initialize / tools/list /
  tools/call). The model can read only what its capsule pins.

Both require ``REPO_MCP_TOKEN``; without it, or with a wrong bearer, every route
but ``/health`` is a 404 — the door does not exist.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import mirror, tools

PROTOCOL_VERSION = "2025-06-18"
CAPSULE_TTL_S = 3 * 3600
MAX_CAPSULES = 500

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_capsules: dict[str, tuple[float, tools.Capsule]] = {}
_capsules_guard = threading.Lock()


def _authorized(request: Request) -> bool:
    expected = os.environ.get("REPO_MCP_TOKEN", "").strip()
    got = request.headers.get("authorization", "")
    return bool(expected) and hmac.compare_digest(got, f"Bearer {expected}")


def _mint(cap: tools.Capsule) -> str:
    now = time.time()
    capsule_id = secrets.token_urlsafe(24)
    with _capsules_guard:
        for key in [k for k, (exp, _) in _capsules.items() if exp < now]:
            del _capsules[key]
        if len(_capsules) >= MAX_CAPSULES:
            del _capsules[min(_capsules, key=lambda k: _capsules[k][0])]
        _capsules[capsule_id] = (now + CAPSULE_TTL_S, cap)
    return capsule_id


def _lookup(capsule_id: str) -> tools.Capsule | None:
    with _capsules_guard:
        entry = _capsules.get(capsule_id)
    if entry is None or entry[0] < time.time():
        return None
    return entry[1]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/prepare")
async def prepare(request: Request) -> Response:
    if not _authorized(request):
        return Response(status_code=404)
    body = await request.json()
    repo, base, head = (str(body.get(k, "")) for k in ("repo", "base_sha", "head_sha"))
    github_token = request.headers.get("x-github-token", "")
    try:
        await run_in_threadpool(mirror.prepare, repo, base, head, github_token)
    except mirror.MirrorError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    capsule_id = _mint(tools.Capsule(repo=repo, base_sha=base, head_sha=head))
    return JSONResponse({"capsule": capsule_id, "expires_in": CAPSULE_TTL_S})


def _rpc(id_: Any, result: dict[str, Any] | None = None, error: tuple[int, str] | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
    if error:
        payload["error"] = {"code": error[0], "message": error[1]}
    else:
        payload["result"] = result
    return JSONResponse(payload)


@app.post("/mcp/{capsule_id}")
async def mcp(capsule_id: str, request: Request) -> Response:
    if not _authorized(request):
        return Response(status_code=404)
    cap = _lookup(capsule_id)
    if cap is None:
        return Response(status_code=404)
    body = await request.json()
    method, id_ = body.get("method"), body.get("id")
    if id_ is None:
        return Response(status_code=202)
    if method == "initialize":
        return _rpc(id_, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "repo", "version": "1.0.0"}})
    if method == "tools/list":
        return _rpc(id_, {"tools": [t.schema() for t in tools.TOOLS.values()]})
    if method == "tools/call":
        params = dict(body.get("params") or {})
        text, is_error = await run_in_threadpool(
            tools.call, cap, str(params.get("name")), dict(params.get("arguments") or {}))
        return _rpc(id_, {"content": [{"type": "text", "text": text}], "isError": is_error})
    if method == "ping":
        return _rpc(id_, {})
    return _rpc(id_, error=(-32601, f"method {method!r} not supported"))
