"""Start Work modal — kills the dashboard → terminal → IDE copy-paste tax.

When the dev clicks an issue card in the AI Resolve carousel, instead of
just dispatching the autonomous agent we open a modal that hands them
three things, each with a Copy button:

  1. The branch name (per Linear's `branchName` convention, computed by
     the snapshot generator: `bernarduriza/visal-<seq>-<slug>`)
  2. The git command to create + check out that branch from the right base
     (front → main, back → staging-v2, both/other → main)
  3. A starter prompt for Cursor/Claude Code that names the issue + URL
     + a 5-step recipe so the agent doesn't drift

The modal is rendered lazily on first card click — DOM stays empty until
needed, no bloat for users who only use the carousel for dispatch.

Lives in its own module so dispatch.py stays focused on dispatch.
"""

from browser import document, html, window


_MODAL_ID = "start-work-modal"


def _git_base_branch_for(scope: str) -> str:
    """Pick the right git base branch for a `git checkout -b` command.

    Per ~/.claude/rules/git.md: backend default is `staging-v2`, frontend is
    `main`. For "both" issues default to `main` (frontend usually leads),
    operator can edit before pasting.
    """
    return "staging-v2" if scope == "back" else "main"


def _starter_prompt(issue: dict) -> str:
    """Spanish-language starter prompt the dev pastes into Cursor/Claude Code.

    Bernard's preferred shape: name the ticket, hand the URL, give a 5-step
    recipe that ends in a verified build. Keeps the agent from wandering.
    """
    ident = issue.get("identifier", "?")
    title = issue.get("title", "")
    url = issue.get("url", "#")
    branch = issue.get("branch", "")
    scope = issue.get("scope", "other")
    repo_hint = "frontend" if scope == "front" else "backend" if scope == "back" else "frontend o backend (revisá ambos)"

    return (
        f"Trabajá en {ident}: {title}\n\n"
        f"Branch: {branch}\n"
        f"Repo objetivo (probable): {repo_hint}\n"
        f"Plane: {url}\n\n"
        "Pasos:\n"
        f"1. Leé el ticket completo en {url}\n"
        f"2. Buscá código relevante con grep/glob basado en el título: \"{title}\"\n"
        "3. Proponé un plan corto (3-5 bullets) antes de tocar nada\n"
        "4. Aplicá la mínima fix posible — sin refactor de paso\n"
        "5. Verde tsc + tests + un curl manual del flow afectado antes de commit\n"
    )


def _git_command(issue: dict) -> str:
    branch = issue.get("branch", "")
    base = _git_base_branch_for(issue.get("scope", "other"))
    return f"git fetch origin && ALLOW_FEATURE_BRANCH=1 git checkout -b {branch} origin/{base}"


def open_modal(issue: dict) -> None:
    """Render and show the Start Work modal for the given issue dict."""
    overlay = _ensure_modal_root()
    overlay.clear()

    panel = html.DIV(Class="start-work-panel")

    # Header
    head = html.DIV(Class="start-work-head")
    head_left = html.DIV(Class="start-work-head-left")
    head_left <= html.SPAN(issue.get("identifier", "?"), Class="start-work-id")
    scope = (issue.get("scope") or "other").lower()
    head_left <= html.SPAN(scope.upper(), Class=f"scope-badge-{scope}")
    head <= head_left
    close_btn = html.BUTTON("×", Class="start-work-close", title="Close")
    close_btn.bind("click", lambda ev: close_modal())
    head <= close_btn
    panel <= head

    panel <= html.P(issue.get("title", ""), Class="start-work-title")

    # Section helpers
    panel <= _copy_section("Branch", issue.get("branch", ""))
    panel <= _copy_section("Git command", _git_command(issue), pre=True)
    panel <= _copy_section("Starter prompt (paste in Cursor / Claude Code)",
                           _starter_prompt(issue), pre=True, big=True)

    # Footer link out
    foot = html.DIV(Class="start-work-foot")
    open_link = html.A("Open in Plane ↗", href=issue.get("url", "#"),
                       target="_blank", Class="start-work-link")
    foot <= open_link
    panel <= foot

    overlay <= panel
    overlay.style.display = "flex"


def close_modal(_ev=None) -> None:
    overlay = document.querySelector(f"#{_MODAL_ID}")
    if overlay is not None:
        overlay.style.display = "none"


def _ensure_modal_root():
    """Lazily create the overlay container the first time the modal opens.

    Brython's `document["id"]` raises KeyError on missing elements (it's a
    mapping __getitem__, not the JS bracket-notation querySelector).
    document.querySelector("#id") returns None on miss, which matches the
    original is-not-None semantics. Returns the overlay so callers don't
    have to do a second lookup.
    """
    existing = document.querySelector(f"#{_MODAL_ID}")
    if existing is not None:
        return existing
    overlay = html.DIV(id=_MODAL_ID, Class="start-work-overlay")
    overlay.style.display = "none"
    overlay.bind("click", _on_overlay_click)
    document.body <= overlay


def _on_overlay_click(ev):
    """Close on click outside the panel (overlay itself, not the panel)."""
    if ev.target.id == _MODAL_ID:
        close_modal()


def _copy_section(label: str, value: str, pre: bool = False, big: bool = False):
    section = html.DIV(Class="start-work-section")
    head = html.DIV(Class="start-work-section-head")
    head <= html.SPAN(label, Class="start-work-label")
    btn = html.BUTTON("Copy", Class="start-work-copy-btn")
    btn.bind("click", lambda ev, v=value, b=btn: _copy_to_clipboard(v, b))
    head <= btn
    section <= head

    if pre:
        body_cls = "start-work-pre" + (" start-work-pre-big" if big else "")
        section <= html.PRE(value, Class=body_cls)
    else:
        section <= html.DIV(value, Class="start-work-mono")
    return section


def _copy_to_clipboard(value: str, btn) -> None:
    """Use the Clipboard API; fall back to a hidden textarea + execCommand."""
    try:
        # Brython exposes window.navigator with .clipboard.writeText()
        window.navigator.clipboard.writeText(value)
        _flash(btn, "Copied!")
        return
    except Exception:
        pass
    # Legacy fallback — wrap the value in a textarea, select, execCommand
    try:
        ta = html.TEXTAREA(value)
        ta.style.position = "fixed"
        ta.style.opacity = "0"
        document.body <= ta
        ta.select()
        document.execCommand("copy")
        ta.remove()
        _flash(btn, "Copied!")
    except Exception:
        _flash(btn, "Copy failed")


def _flash(btn, text: str) -> None:
    """Briefly show feedback text on the copy button, then revert."""
    original = btn.text
    btn.text = text
    btn.attrs["disabled"] = "disabled"

    from browser import timer

    def revert():
        btn.text = original
        if "disabled" in btn.attrs:
            del btn.attrs["disabled"]
    timer.set_timeout(revert, 1200)
