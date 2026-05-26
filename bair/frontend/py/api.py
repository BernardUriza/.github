"""GitHub API helpers — thin wrappers around browser.ajax."""

from browser import ajax

from .config import API
from .state import state


def gh_headers():
    return {"Authorization": f"token {state.token}", "Accept": "application/vnd.github+json"}


def gh_get(path, callback):
    req = ajax.Ajax()
    req.open("GET", f"{API}{path}", True)
    for k, v in gh_headers().items():
        req.set_header(k, v)
    req.bind("complete", callback)
    req.send()


def gh_post(path, data, callback):
    import json as _json
    req = ajax.Ajax()
    req.open("POST", f"{API}{path}", True)
    for k, v in gh_headers().items():
        req.set_header(k, v)
    req.set_header("Content-Type", "application/json")
    req.bind("complete", callback)
    req.send(_json.dumps(data))
