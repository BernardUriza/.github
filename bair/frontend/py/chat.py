"""VAIR Chat — minimal MVP chat with GPT-5.5.

Quick win cut: non-streaming POST to OpenAI Chat Completions, render full
response when it arrives. Streaming + tool calls land in a follow-up PR.

Token (BYOK): stored in localStorage as `vair_openai_token`. On first send,
if missing, prompt the user via `window.prompt()` and persist. Reuses the
same browser-only key pattern that already exists for `vair_token` (GH PAT).

Scope deliberately tiny: input + send button + message bubbles. No tool
calls, no system-prompt context injection, no streaming. Everything else
is a follow-up.
"""

import json as _json

from browser import ajax, document, html, window

from .state import state


_STORAGE_OPENAI = "vair_openai_token"
_STORAGE_MINIMIZED = "vair_chat_minimized"
_OPENAI_API = "https://api.openai.com/v1/chat/completions"
# SSOT mirror — must match vair/infra/constants.py:OPENAI_MODEL.
# Cross-runtime (Brython in browser) prevents direct import. If you change
# this, change OPENAI_MODEL in constants.py first, then mirror here.
_MODEL = "gpt-5.5"
_SNAPSHOT_URL = "data/issues.json"  # static JSON written by plane-snapshot workflow

_SYSTEM_PROMPT_BASE = (
    "You are VAIR, a development assistant for the Visalaw engineering team. "
    "You help engineers triage their queue, decide what to work on, and "
    "bootstrap their local environment. Be concise — engineers prefer short "
    "answers with concrete next steps over essays. When you don't know "
    "something, say so explicitly and suggest where to find it."
    "\n\n"
    "## Security & privacy rules — non-negotiable\n"
    "1. Never ask the user to paste tokens, API keys, passwords, OAuth "
    "secrets, OTP codes, session cookies, or any other credential. If they "
    "need to configure a service, point them at the relevant settings "
    "page; never receive the secret value yourself.\n"
    "2. If the user pastes what looks like a secret (patterns: ghp_*, "
    "gho_*, github_pat_*, sk-*, plane_api_*, xoxb-*, base64 JWT, AWS "
    "AKIA*, hex tokens > 20 chars), refuse to acknowledge or repeat the "
    "value. Tell them it should be revoked + rotated and stop.\n"
    "3. Visalaw handles immigration data covered by attorney-client "
    "privilege. Never request, store, or repeat any personally "
    "identifiable information about clients: A-numbers, passports, DOB, "
    "addresses, case numbers, full names, country of origin, employer, "
    "family relationships. If the user pastes such data, refuse to "
    "process it and ask them to redact before continuing.\n"
    "4. Never invent file paths, function names, commit SHAs, PR numbers, "
    "or Plane issue IDs. If you don't have direct evidence in the "
    "snapshot below or in the user's message, say 'I don't have that "
    "information' and suggest where to find it (gh CLI, app.plane.so, "
    "the repo).\n"
    "5. When citing a Plane issue, always use the form VISAL-<id> "
    "alongside the full URL https://app.plane.so/visalaw-ai/browse/"
    "VISAL-<id>/ so the engineer can click through. Issue IDs without a "
    "URL are useless context.\n"
    "6. When citing GitHub PRs or commits, use the full URL "
    "(https://github.com/Visalaw/<repo>/pull/<n> or "
    "https://github.com/Visalaw/<repo>/commit/<sha>), not shorthand "
    "like #123 or 7a262ef. The engineer copy-pastes your output into "
    "Slack and Plane; bare numbers lose context."
)

# Hydrated at panel-init time from `frontend/data/issues.json` (Plane snapshot
# regenerated every 15 min by `.github/workflows/plane-snapshot.yml`). Becomes
# part of the system prompt so the model can answer "what's in the queue?"
# without needing browser-direct access to api.plane.so (CORS blocks it).
_queue_context: str = ""

# In-memory conversation — does NOT persist across reloads. That's intentional
# for the MVP: each session is a fresh greeting.
_messages: list = []

# Module-level busy flag — guards against concurrent sends even if the UI
# disabled state were bypassed (slash-commands, programmatic dispatch, etc.).
_busy: bool = False

# Reference to a transient "Thinking…" bubble while waiting for OpenAI.
# Removed in _on_response so the final answer takes its place.
_thinking_bubble = None

# Snapshot loading state — flags prevent duplicate fetches when render_chat()
# is invoked more than once (e.g. after logout/login cycle) and let the header
# show an accurate "Queue: ready / loading / unavailable" pill.
_snapshot_state: str = "idle"  # "idle" | "loading" | "ready" | "unavailable"
_snapshot_generated_at: str = ""

# Quick-prompt chips rendered above the input row. (label, prompt) pairs.
# Keep the prompts short — the engineer can edit before sending if the chip
# only pre-populates, but right now click = send.
_QUICK_CHIPS = [
    ("Queue summary", "Give me a one-paragraph summary of the current Plane queue."),
    ("Top priority", "Based on the snapshot, what should I work on next? Pick one issue and justify the choice in two sentences."),
    ("Repo bootstrap", "What commands do I need to bootstrap the frontend-core-2.0 repo from scratch on a fresh machine?"),
]

# Slash command dispatch — handlers must accept no args and return nothing.
# /clear /refresh /key — resolved locally, never sent to OpenAI.
_SLASH_HELP = (
    "Slash commands:\n"
    "• `/clear` — reset this conversation\n"
    "• `/refresh` — reload the Plane queue snapshot\n"
    "• `/key` — open the OpenAI key modal (setup or reset)\n"
    "• `/help` — show this list"
)


def _truncate_words(text, max_chars):
    """Truncate `text` at the last word boundary <= max_chars. Appends … if cut."""
    if len(text) <= max_chars:
        return text
    cut = text.rfind(" ", 0, max_chars)
    if cut < max_chars // 2:  # word too long, fall back to hard cut
        cut = max_chars
    return text[:cut].rstrip() + "…"


def _load_queue_snapshot(force=False):
    """Fetch `data/issues.json` (server-generated Plane snapshot) and stash a
    summary into module-level `_queue_context` for the system prompt.

    Idempotent: if a load is already in flight or ready, returns immediately
    unless `force=True` (used by the Refresh button). Same-origin fetch — no
    CORS. On failure, leaves `_snapshot_state` = "unavailable" so the header
    can show the bad state instead of pretending we have data.
    """
    global _snapshot_state
    if _snapshot_state == "loading":
        return
    if _snapshot_state == "ready" and not force:
        return
    _snapshot_state = "loading"
    _update_snapshot_pill()
    req = ajax.Ajax()
    req.open("GET", _SNAPSHOT_URL, True)
    req.bind("complete", _on_snapshot_loaded)
    req.send()


def _on_snapshot_loaded(req):
    global _queue_context, _snapshot_state, _snapshot_generated_at
    if req.status != 200:
        _snapshot_state = "unavailable"
        _update_snapshot_pill()
        return
    try:
        data = _json.loads(req.text)
    except Exception:
        _snapshot_state = "unavailable"
        _update_snapshot_pill()
        return
    total = data.get("total_open", 0)
    by_group = data.get("by_group", {}) or {}
    top = data.get("top", []) or []
    generated = data.get("generated_at", "")
    _snapshot_generated_at = generated

    by_group_line = ", ".join(f"{k}:{v}" for k, v in by_group.items()) or "no data"
    top_lines = []
    for item in top[:10]:
        ident = item.get("identifier", "?")
        title = _truncate_words(item.get("title", "") or "", 80)
        st = item.get("state", "")
        scope = item.get("scope", "")
        url = item.get("url", "") or f"https://app.plane.so/visalaw-ai/browse/{ident}/"
        scope_tag = f" ({scope})" if scope else ""
        top_lines.append(f"  - {ident} [{st}]{scope_tag} {title} → {url}")

    _queue_context = (
        f"## Plane queue snapshot (regenerated every 15 min)\n"
        f"Generated: {generated}\n"
        f"Open issues: {total} ({by_group_line})\n"
        f"Top of backlog (first {len(top_lines)}):\n"
        + "\n".join(top_lines)
        + "\n\n"
        "When the user asks about the queue, refer to this data. Don't say "
        "you have no access — you do, via this snapshot. Always cite issues "
        "with the full URL shown above so the engineer can click through. "
        "The scope tag (back/front/both/other) indicates which side of the "
        "stack owns the issue."
    )
    _snapshot_state = "ready"
    _update_snapshot_pill()


def _update_snapshot_pill():
    """Reflect _snapshot_state in the header pill, if the chat is rendered."""
    if "chat-snapshot-pill" not in document:
        return
    pill = document["chat-snapshot-pill"]
    label = {
        "idle": "Queue: pending",
        "loading": "Queue: loading…",
        "ready": "Queue: ready",
        "unavailable": "Queue: unavailable",
    }.get(_snapshot_state, "Queue: ?")
    pill.text = label
    pill.attrs["class"] = f"chat-snapshot-pill chat-snapshot-{_snapshot_state}"
    if _snapshot_generated_at:
        pill.attrs["title"] = f"Generated: {_snapshot_generated_at}"


def _on_refresh_snapshot_click(ev=None):
    _load_queue_snapshot(force=True)


def render_chat():
    """Build the chat panel as a fixed bottom-right floating widget."""
    panel = document["vair-chat"]
    if panel is None:
        return
    panel.clear()

    # Fire-and-forget: hydrate _queue_context in background. Idempotent —
    # if a previous render already loaded it, this is a no-op. The header
    # pill reflects the actual state instead of pretending we have data.
    _load_queue_snapshot()

    # Header
    head = html.DIV(Class="chat-head")
    head <= html.SPAN("VAIR Chat", Class="chat-title")
    head <= html.SPAN(_MODEL, Class="chat-model")
    pill = html.SPAN("Queue: pending",
                     id="chat-snapshot-pill",
                     Class=f"chat-snapshot-pill chat-snapshot-{_snapshot_state}")
    if _snapshot_generated_at:
        pill.attrs["title"] = f"Generated: {_snapshot_generated_at}"
    head <= pill
    minimize_btn = html.BUTTON("−", id="chat-minimize", Class="chat-head-btn",
                                **{"aria-label": "Minimize chat",
                                   "title": "Minimize chat"})
    minimize_btn.bind("click", _on_minimize_click)
    head <= minimize_btn
    refresh_btn = html.BUTTON("↻", id="chat-refresh", Class="chat-head-btn",
                              **{"aria-label": "Refresh queue snapshot",
                                 "title": "Refresh queue snapshot"})
    refresh_btn.bind("click", _on_refresh_snapshot_click)
    head <= refresh_btn
    key_btn = html.BUTTON("Key", id="chat-key-btn", Class="chat-head-btn",
                          **{"aria-label": "Manage OpenAI key",
                             "title": "Manage OpenAI key"})
    key_btn.bind("click", _on_key_button_click)
    head <= key_btn
    clear_btn = html.BUTTON("Clear", id="chat-clear", Class="chat-head-btn",
                            **{"aria-label": "Clear conversation"})
    clear_btn.bind("click", _on_clear_click)
    head <= clear_btn
    panel <= head

    # Sync pill state in case _on_snapshot_loaded fired before render_chat()
    # rebuilt the DOM (e.g. after logout/login when snapshot was already loaded).
    _update_snapshot_pill()

    # Message stream container — welcome line tracks the snapshot state honestly
    # rather than claiming access before it's hydrated.
    welcome = (
        "Hola. Soy VAIR. " +
        {
            "ready": "Tengo cargado el snapshot de tu queue de Plane. Pregúntame qué hay en el backlog, qué trabajar primero, o cualquier cosa del repo.",
            "loading": "Estoy cargando el snapshot de tu queue. Puedes empezar a escribir y para cuando lo mandes ya estará listo.",
            "unavailable": "El snapshot de Plane no está disponible ahora mismo (data/issues.json falló). Aun así te ayudo con el repo y comandos. Usa ↻ para reintentar.",
            "idle": "Te ayudo con el repo y tu queue de Plane. Snapshot cargando en background.",
        }[_snapshot_state]
    )
    stream = html.DIV(id="chat-stream", Class="chat-stream",
                      **{"role": "log", "aria-live": "polite"})
    stream <= _bubble("assistant", welcome)
    panel <= stream

    # Restore minimized state from localStorage (persists across reloads).
    _apply_minimized(window.localStorage.getItem(_STORAGE_MINIMIZED) == "1")

    # Quick-prompt chips — one click sends a canned question.
    chips = html.DIV(id="chat-chips", Class="chat-chips")
    for label, prompt in _QUICK_CHIPS:
        chip = html.BUTTON(label, Class="chat-chip", **{"data-prompt": prompt})
        chip.bind("click", _on_chip_click)
        chips <= chip
    panel <= chips

    # Input row — textarea allows Shift+Enter newlines and word wrap.
    row = html.DIV(Class="chat-input-row")
    box = html.TEXTAREA(
        id="chat-input",
        placeholder="Pregúntale algo a VAIR… (Shift+Enter for newline)",
        Class="chat-input",
        rows="2",
    )
    box.bind("keydown", _on_keydown)
    btn = html.BUTTON("Send", id="chat-send", Class="chat-send-btn",
                      **{"aria-label": "Send message"})
    btn.bind("click", _on_send_click)
    row <= box
    row <= btn
    panel <= row


_SAFE_TAGS = [
    "a", "p", "br", "strong", "em", "code", "pre",
    "ul", "ol", "li", "blockquote", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
]
_SAFE_ATTRS = ["href", "title", "target", "rel"]


def _sanitize_markdown(text):
    """Render markdown -> sanitized HTML. Returns None if DOMPurify is unavailable;
    caller must fall back to plain text. Forces target=_blank + rel=noopener on anchors."""
    if not hasattr(window, "DOMPurify"):
        return None
    try:
        raw = window.marked.parse(text)
    except Exception:
        return None
    try:
        clean = window.DOMPurify.sanitize(raw, {
            "ALLOWED_TAGS": _SAFE_TAGS,
            "ALLOWED_ATTR": _SAFE_ATTRS,
            "ALLOW_DATA_ATTR": False,
        })
    except Exception:
        return None
    return clean


_COPY_BUTTON_MIN_LEN = 200


def _bubble(role, text):
    div = html.DIV(Class=f"chat-bubble chat-{role}")
    clean = _sanitize_markdown(text)
    if clean is None:
        # DOMPurify missing or sanitize failed — fall back to plain text (escaped via textContent)
        div <= html.SPAN(text)
    else:
        div.innerHTML = clean
        # Post-sanitize: open external links in a new tab with noopener
        for a in div.querySelectorAll("a[href]"):
            a.setAttribute("target", "_blank")
            a.setAttribute("rel", "noopener noreferrer")

    # Copy button on long assistant replies — easy to grab snippets out of
    # markdown without selecting by hand. Only for assistant (user has the
    # text already; error/thinking are transient).
    if role == "assistant" and len(text) >= _COPY_BUTTON_MIN_LEN:
        copy_btn = html.BUTTON("Copy", Class="chat-copy-btn",
                                **{"aria-label": "Copy message",
                                   "data-text": text})
        copy_btn.bind("click", _on_copy_click)
        div <= copy_btn
    return div


def _on_copy_click(ev):
    btn = ev.currentTarget
    text = btn.getAttribute("data-text") or ""
    try:
        window.navigator.clipboard.writeText(text)
        btn.text = "Copied"
        btn.classList.add("copied")
        def reset(_):
            btn.text = "Copy"
            btn.classList.remove("copied")
        window.setTimeout(reset, 1500)
    except Exception:
        pass


def _on_keydown(ev):
    # Enter sends; Shift+Enter keeps default (newline in textarea).
    if ev.key == "Enter" and not ev.shiftKey:
        ev.preventDefault()
        _on_send_click(ev)


def _on_clear_click(ev=None):
    global _messages
    if _busy:
        return
    _messages = []
    stream = document["chat-stream"] if "chat-stream" in document else None
    if stream is None:
        return
    stream.clear()
    stream <= _bubble(
        "assistant",
        "Conversation cleared. Pregúntame lo que necesites.",
    )


def reset_session():
    """Public reset hook called by auth.logout(). Wipes in-memory conversation
    so the next session starts fresh (different user, different billing).
    Does NOT touch localStorage tokens — auth.logout owns that side."""
    global _messages, _busy, _thinking_bubble, _queue_context, _snapshot_state, _snapshot_generated_at
    _messages = []
    _busy = False
    _thinking_bubble = None
    _queue_context = ""
    _snapshot_state = "idle"
    _snapshot_generated_at = ""
    if "vair-chat" in document:
        document["vair-chat"].clear()


def _on_chip_click(ev):
    if _busy or "chat-input" not in document:
        return
    prompt = ev.target.getAttribute("data-prompt") or ev.target.text
    document["chat-input"].value = prompt
    _on_send_click(ev)


def _handle_slash(text):
    """Return True if the input was a slash command and was handled locally."""
    cmd = text.lstrip("/").split(None, 1)[0].lower() if text.startswith("/") else None
    if cmd is None:
        return False
    if cmd == "clear":
        _on_clear_click()
        return True
    if cmd == "refresh":
        _load_queue_snapshot(force=True)
        _append_bubble("assistant", "Queue snapshot refresh queued. Pill shows the live state.")
        return True
    if cmd == "key":
        _on_key_button_click()
        return True
    if cmd == "help":
        _append_bubble("assistant", _SLASH_HELP)
        return True
    _append_bubble("error", f"Unknown slash command: `/{cmd}`. Type `/help` for the list.")
    return True


def _on_send_click(ev=None):
    if _busy:
        return
    if "chat-input" not in document:
        return
    box = document["chat-input"]
    text = (box.value or "").strip()
    if not text:
        return
    if _handle_slash(text):
        box.value = ""
        box.focus()
        return
    if len(text) > 4000:
        _append_bubble(
            "error",
            f"Message too long ({len(text)} chars). Limit is 4000.",
        )
        return

    token = _ensure_token()
    if not token:
        # _ensure_token already opened the key modal. Surface the reason
        # in the stream so the user knows the message wasn't sent.
        _append_bubble(
            "error",
            "OpenAI key needed. Paste one in the modal above and resend.",
        )
        return

    box.value = ""
    _set_busy(True)
    _append_bubble("user", text)
    _messages.append({"role": "user", "content": text})
    _show_thinking()

    system_prompt = _SYSTEM_PROMPT_BASE
    if _queue_context:
        system_prompt = f"{_SYSTEM_PROMPT_BASE}\n\n{_queue_context}"

    payload = {
        "model": _MODEL,
        "messages": [{"role": "system", "content": system_prompt}] + _messages,
        "stream": False,
    }

    req = ajax.Ajax()
    req.open("POST", _OPENAI_API, True)
    req.set_header("Authorization", f"Bearer {token}")
    req.set_header("Content-Type", "application/json")
    req.bind("complete", _on_response)
    req.send(_json.dumps(payload))


def _on_response(req):
    _set_busy(False)
    # Restore focus to the textarea so the user can keep typing.
    if "chat-input" in document:
        document["chat-input"].focus()

    # Error paths drop the pending bubble and surface an .chat-error.
    # Happy path keeps the pending bubble and morphs it into the final
    # assistant message — sets up streaming where deltas flow through
    # _update_pending() and the same bubble survives chunk-by-chunk.
    if req.status == 401:
        _remove_thinking()
        window.localStorage.removeItem(_STORAGE_OPENAI)
        state.openai_token = ""
        _append_bubble(
            "error",
            "OpenAI API key inválido. Lo borré — el próximo mensaje te pedirá uno nuevo.",
        )
        return
    if req.status == 429:
        _remove_thinking()
        _append_bubble(
            "error",
            "Rate limit hit (HTTP 429). Espera unos segundos antes de mandar otro.",
        )
        return
    if req.status == 0:
        _remove_thinking()
        _append_bubble(
            "error",
            "Network error or timeout. Revisa tu conexión y vuelve a intentar.",
        )
        return
    if req.status != 200:
        _remove_thinking()
        try:
            err = _json.loads(req.text).get("error", {}).get("message", f"HTTP {req.status}")
        except Exception:
            err = f"HTTP {req.status}"
        _append_bubble("error", f"OpenAI error: {err}")
        return

    try:
        data = _json.loads(req.text)
        reply = data["choices"][0]["message"]["content"]
    except Exception as e:
        _remove_thinking()
        _append_bubble("error", f"Respuesta malformada: {e}")
        return

    _messages.append({"role": "assistant", "content": reply})
    _finalize_pending(reply)


# Streaming-ready pending bubble API.
#
# Today the chat is non-streaming: _show_pending() shows "Thinking…",
# _finalize_pending(text) replaces it with the full sanitized markdown.
#
# When streaming lands, the same handle survives delta arrivals via
# _update_pending(partial_text), which re-renders the markdown each chunk
# while honoring _is_near_bottom() so the user can scroll up without being
# yanked back to the bottom on every token.
#
# Keep this contract stable. The streaming patch should NOT need to touch
# the surface — only swap the ajax 'complete' handler for an SSE/fetch
# stream reader that calls _update_pending() per chunk.

def _show_pending(initial_label="Thinking…"):
    """Append a placeholder assistant bubble. Returns nothing; the handle
    lives in module-level _thinking_bubble."""
    global _thinking_bubble
    if "chat-stream" not in document:
        return
    bubble = _bubble("thinking", initial_label)
    stream = document["chat-stream"]
    stream <= bubble
    stream.scrollTop = stream.scrollHeight
    _thinking_bubble = bubble


def _update_pending(text):
    """Replace the pending bubble content with sanitized markdown of the
    full text-so-far. Autoscrolls only when the user was already pinned
    to the bottom (no yank-back on every streamed chunk).

    Currently unused — wired and ready for streaming follow-up."""
    if _thinking_bubble is None or "chat-stream" not in document:
        return
    near_bottom = _is_near_bottom(document["chat-stream"])
    clean = _sanitize_markdown(text)
    if clean is None:
        _thinking_bubble.text = text
    else:
        _thinking_bubble.innerHTML = clean
        for a in _thinking_bubble.querySelectorAll("a[href]"):
            a.setAttribute("target", "_blank")
            a.setAttribute("rel", "noopener noreferrer")
    if near_bottom:
        document["chat-stream"].scrollTop = document["chat-stream"].scrollHeight


def _finalize_pending(text):
    """Replace the pending bubble with the final sanitized response and
    drop the .chat-thinking class so the bubble adopts assistant styling.
    Caller is still responsible for appending the message to _messages."""
    global _thinking_bubble
    if _thinking_bubble is None:
        # No pending — just append a fresh assistant bubble.
        _append_bubble("assistant", text)
        return
    # Build the final bubble (includes copy button when long) and swap it in.
    final = _bubble("assistant", text)
    try:
        _thinking_bubble.replaceWith(final)
    except Exception:
        _thinking_bubble.remove()
        if "chat-stream" in document:
            document["chat-stream"] <= final
    _thinking_bubble = None
    if "chat-stream" in document and _is_near_bottom(document["chat-stream"], threshold=120):
        document["chat-stream"].scrollTop = document["chat-stream"].scrollHeight


# Back-compat aliases for the rest of the module — these names are still
# referenced by _on_send_click / _on_response. Remove in a follow-up once
# all call sites are migrated.
_show_thinking = _show_pending


def _remove_thinking():
    """Drop the pending bubble without rendering a final answer. Used on
    error paths where the next action is _append_bubble('error', ...)."""
    global _thinking_bubble
    if _thinking_bubble is not None:
        try:
            _thinking_bubble.remove()
        except Exception:
            pass
        _thinking_bubble = None


def _append_bubble(role, text):
    if "chat-stream" not in document:
        return
    stream = document["chat-stream"]
    # Capture scroll position BEFORE appending so we know whether the user
    # was reading history or already pinned to the bottom.
    near_bottom = _is_near_bottom(stream)
    stream <= _bubble(role, text)
    # Only autoscroll if the user was already at/near the bottom. If they
    # scrolled up to re-read something, respect that and let the new bubble
    # land below without yanking their viewport.
    if near_bottom:
        stream.scrollTop = stream.scrollHeight


def _is_near_bottom(stream, threshold=48):
    """True iff the stream is scrolled within `threshold` px of the bottom.
    Also true if the content fits without scrolling (scrollHeight ≈ clientHeight)."""
    try:
        distance = stream.scrollHeight - stream.scrollTop - stream.clientHeight
        return distance <= threshold
    except Exception:
        return True


def _on_minimize_click(ev=None):
    minimized = window.localStorage.getItem(_STORAGE_MINIMIZED) == "1"
    _apply_minimized(not minimized, persist=True)


def _apply_minimized(minimized, persist=False):
    panel = document["vair-chat"] if "vair-chat" in document else None
    if panel is None:
        return
    btn = document["chat-minimize"] if "chat-minimize" in document else None
    if minimized:
        panel.classList.add("minimized")
        if btn is not None:
            btn.text = "▢"
            btn.attrs["aria-label"] = "Expand chat"
            btn.attrs["title"] = "Expand chat"
    else:
        panel.classList.remove("minimized")
        if btn is not None:
            btn.text = "−"
            btn.attrs["aria-label"] = "Minimize chat"
            btn.attrs["title"] = "Minimize chat"
    if persist:
        window.localStorage.setItem(_STORAGE_MINIMIZED, "1" if minimized else "0")


def _set_busy(busy):
    global _busy
    _busy = busy
    # Both the textarea and the send button get disabled — CSS handles the
    # visual state via .chat-input:disabled and .chat-send-btn:disabled.
    for el_id, idle_text in (("chat-send", "Send"), ("chat-input", None)):
        if el_id not in document:
            continue
        el = document[el_id]
        if busy:
            el.attrs["disabled"] = "disabled"
            if idle_text is not None:
                el.text = "…"
        else:
            if "disabled" in el.attrs:
                del el.attrs["disabled"]
            if idle_text is not None:
                el.text = idle_text
    # Chips reflect busy too so the user can't queue concurrent sends.
    if "chat-chips" in document:
        for chip in document["chat-chips"].querySelectorAll(".chat-chip"):
            if busy:
                chip.setAttribute("disabled", "disabled")
            else:
                chip.removeAttribute("disabled")


def _ensure_token():
    """Return the OpenAI token if already set, otherwise open the in-panel
    key modal and return empty string. The caller must handle '' gracefully
    (typically: show a bubble saying 'configure your key first').

    Order of resolution:
      1. state.openai_token (already loaded this session)
      2. localStorage `vair_openai_token` (loaded once, then cached in state)
      3. None — opens the modal; user pastes a key and retries.
    """
    if state.openai_token:
        return state.openai_token
    saved = window.localStorage.getItem(_STORAGE_OPENAI)
    if saved:
        state.openai_token = saved
        return saved
    _open_key_modal()
    return ""


def _open_key_modal(mode="setup"):
    """Render the key modal as a panel-scoped overlay.

    mode='setup'  → empty input, Save button, copy explaining local-only storage
    mode='reset'  → shows masked saved key, Forget button, Cancel
    """
    panel = document["vair-chat"]
    if panel is None:
        return
    # Remove any existing modal first (idempotent)
    if "chat-key-modal" in document:
        document["chat-key-modal"].remove()

    overlay = html.DIV(id="chat-key-modal", Class="chat-key-modal")

    if mode == "reset":
        masked = _mask_token(state.openai_token or window.localStorage.getItem(_STORAGE_OPENAI) or "")
        overlay <= html.H3("OpenAI key", Class="chat-key-title")
        overlay <= html.P(
            f"Saved key: {masked} — stored only in this browser's "
            "localStorage. Forgetting it does not revoke it on OpenAI's "
            "side; rotate the key in your OpenAI dashboard if you suspect "
            "it leaked.",
            Class="chat-key-desc",
        )
        actions = html.DIV(Class="chat-key-actions")
        forget = html.BUTTON("Forget key", Class="chat-key-btn chat-key-btn-danger")
        forget.bind("click", _on_key_forget_click)
        cancel = html.BUTTON("Cancel", Class="chat-key-btn")
        cancel.bind("click", _close_key_modal)
        actions <= forget
        actions <= cancel
        overlay <= actions
    else:
        overlay <= html.H3("Set OpenAI key", Class="chat-key-title")
        overlay <= html.P(
            "Paste a key starting with sk-. It is stored only in this "
            "browser's localStorage — never sent to any Visalaw server "
            "and never leaves your machine except to call OpenAI.",
            Class="chat-key-desc",
        )
        inp = html.INPUT(
            id="chat-key-input",
            type="password",
            placeholder="sk-…",
            Class="chat-key-input",
            spellcheck="false",
            autocomplete="off",
        )
        inp.bind("keydown", _on_key_modal_keydown)
        overlay <= inp
        err = html.DIV(id="chat-key-error", Class="chat-key-error")
        overlay <= err
        actions = html.DIV(Class="chat-key-actions")
        save = html.BUTTON("Save", Class="chat-key-btn chat-key-btn-primary")
        save.bind("click", _on_key_save_click)
        cancel = html.BUTTON("Cancel", Class="chat-key-btn")
        cancel.bind("click", _close_key_modal)
        actions <= save
        actions <= cancel
        overlay <= actions

    panel <= overlay
    # Focus the input if present
    if mode == "setup" and "chat-key-input" in document:
        document["chat-key-input"].focus()


def _close_key_modal(ev=None):
    if "chat-key-modal" in document:
        document["chat-key-modal"].remove()


def _mask_token(token):
    if not token or len(token) < 10:
        return "(none)"
    return f"{token[:6]}…{token[-4:]}"


def _on_key_modal_keydown(ev):
    if ev.key == "Enter":
        ev.preventDefault()
        _on_key_save_click(ev)
    elif ev.key == "Escape":
        ev.preventDefault()
        _close_key_modal()


def _on_key_save_click(ev=None):
    if "chat-key-input" not in document:
        return
    raw = (document["chat-key-input"].value or "").strip()
    err_el = document["chat-key-error"] if "chat-key-error" in document else None

    def show_err(msg):
        if err_el is not None:
            err_el.text = msg

    if not raw:
        show_err("Paste a key first.")
        return
    if not raw.startswith("sk-"):
        show_err("Key should start with sk-…")
        return
    window.localStorage.setItem(_STORAGE_OPENAI, raw)
    state.openai_token = raw
    _close_key_modal()


def _on_key_forget_click(ev=None):
    window.localStorage.removeItem(_STORAGE_OPENAI)
    state.openai_token = ""
    _close_key_modal()
    _append_bubble(
        "assistant",
        "OpenAI key forgotten. The next message will ask for a new one.",
    )


def _on_key_button_click(ev=None):
    has_key = bool(state.openai_token) or bool(window.localStorage.getItem(_STORAGE_OPENAI))
    _open_key_modal("reset" if has_key else "setup")
