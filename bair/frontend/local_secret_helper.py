"""Local-only helper for loading BAIR secrets from ~/.secrets.

This is intentionally NOT part of the GitHub Pages app. Run it only while
developing locally; it serves the Plane PAT to the dashboard at
http://localhost:8765 via a tiny CORS-allowed JSON endpoint.

Listens on http://127.0.0.1:8766 to avoid colliding with the dashboard's
own static server on 8765 (`python -m http.server 8765` from frontend/).

Endpoints:
  GET /health         -> { ok, plane_token_present, github_token_present, token_kind, github_token_kind }
  GET /plane-token    -> { token } | { error }
  GET /github-token   -> { token } | { error }

Token sources:
  ~/.secrets/plane_pat.txt   (format: `plane_api_<hex>`, per ~/.claude/rules/plane.md)
  ~/.secrets/github_pat.txt  (format: `ghp_*`, `github_pat_*`, or fine-grained PAT)
The legacy linear-api-key.txt path is kept as a graceful fallback for
developers who haven't rotated yet.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os


HOST = "127.0.0.1"
PORT = 8766
ALLOWED_ORIGIN = "http://localhost:8765"
TOKEN_PATH = Path.home() / ".secrets" / "plane_pat.txt"
GITHUB_TOKEN_PATH = Path.home() / ".secrets" / "github_pat.txt"
LEGACY_LINEAR_PATH = Path.home() / ".secrets" / "linear-api-key.txt"


def _read_token_file(path: Path, env_keys: set) -> str:
    """Read a token file. Accepts bare token or `KEY=value` lines."""
    token = path.read_text(encoding="utf-8").strip()
    for line in token.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and line.split("=", 1)[0].strip().upper() in env_keys:
            return line.split("=", 1)[1].strip()
        return line
    return ""


def _read_plane_token() -> str:
    return _read_token_file(TOKEN_PATH, {"PLANE_API_KEY", "PLANE_PAT"})


def _read_github_token() -> str:
    return _read_token_file(GITHUB_TOKEN_PATH, {"GITHUB_TOKEN", "GITHUB_PAT", "GH_TOKEN"})


def _wrong_token_kind(token: str) -> str:
    if token.startswith(("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")):
        return "GitHub"
    if token.startswith(("sk-", "sk-proj-")):
        return "OpenAI"
    if token.startswith(("xoxb-", "xoxp-")):
        return "Slack"
    if token.startswith("lin_api_"):
        return "Linear"
    return ""


def _token_kind() -> str:
    if not TOKEN_PATH.exists():
        return "missing"
    try:
        token = _read_plane_token()
    except OSError:
        return "unreadable"
    if not token:
        return "empty"
    wrong = _wrong_token_kind(token)
    if wrong:
        return wrong.lower()
    if token.startswith("plane_api_"):
        return "plane"
    return "possible_plane"


def _github_token_kind() -> str:
    if not GITHUB_TOKEN_PATH.exists():
        return "missing"
    try:
        token = _read_github_token()
    except OSError:
        return "unreadable"
    if not token:
        return "empty"
    if token.startswith(("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")):
        return "github"
    if token.startswith("plane_api_"):
        return "plane"
    if token.startswith(("sk-", "sk-proj-")):
        return "openai"
    return "possible_github"


class Handler(BaseHTTPRequestHandler):
    def _headers(self, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_OPTIONS(self):
        self._headers(204)

    def _serve_token(self, path, reader, wrong_kind_label, expected_prefixes):
        """Generic token-file server. Returns 200 with {token}, or 4xx/5xx error."""
        if not path.exists():
            self._headers(404)
            self.wfile.write(json.dumps({"error": f"missing local secret at ~/.secrets/{path.name}"}).encode("utf-8"))
            return
        try:
            token = reader()
        except OSError:
            self._headers(500)
            self.wfile.write(json.dumps({"error": "could not read local secret"}).encode("utf-8"))
            return
        if not token:
            self._headers(404)
            self.wfile.write(json.dumps({"error": "empty local secret"}).encode("utf-8"))
            return
        # Reject obviously-wrong token types (e.g. GitHub PAT pasted into plane_pat.txt)
        if expected_prefixes and not any(token.startswith(p) for p in expected_prefixes):
            wrong = _wrong_token_kind(token)
            if wrong and wrong.lower() != wrong_kind_label.lower():
                self._headers(422)
                self.wfile.write(json.dumps({"error": f"local secret is a {wrong} token, not {wrong_kind_label}"}).encode("utf-8"))
                return
        self._headers(200)
        self.wfile.write(json.dumps({"token": token}).encode("utf-8"))

    def do_GET(self):
        if self.path == "/health":
            self._headers(200)
            self.wfile.write(json.dumps({
                "ok": True,
                "plane_token_present": TOKEN_PATH.exists(),
                "github_token_present": GITHUB_TOKEN_PATH.exists(),
                "token_kind": _token_kind(),
                "github_token_kind": _github_token_kind(),
            }).encode("utf-8"))
            return
        if self.path == "/plane-token":
            self._serve_token(TOKEN_PATH, _read_plane_token, "Plane", ("plane_api_",))
            return
        if self.path == "/github-token":
            self._serve_token(
                GITHUB_TOKEN_PATH,
                _read_github_token,
                "GitHub",
                ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
            )
            return
        self._headers(404)
        self.wfile.write(json.dumps({"error": "not found"}).encode("utf-8"))

    def log_message(self, fmt, *args):
        if os.environ.get("VAIR_SECRET_HELPER_DEBUG"):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"BAIR local secret helper listening on http://{HOST}:{PORT}")
    print(f"  Token source: {TOKEN_PATH}")
    print(f"  Allowed origin: {ALLOWED_ORIGIN}")
    server.serve_forever()
