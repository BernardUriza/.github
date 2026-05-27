# Frontend: Brython, Not JavaScript

The BAIR observability dashboard runs Python in the browser via Brython. This is deliberate — do not refactor to JavaScript / TypeScript / React.

## The Hard Rule

Logic in `bair/frontend/py/*.py` runs IN THE BROWSER through Brython's runtime. The HTML pages load Brython then execute the Python source directly:

```html
<script src="https://cdn.jsdelivr.net/npm/brython@…/brython.min.js"></script>
<script type="text/python" src="py/app.py"></script>
<body onload="brython({debug: 1})">
```

If you find yourself wanting to translate a `.py` file to `.js` to "modernize", STOP. The whole stack (xair framework, bair package, frontend dashboard) speaks one language. Adding a JS layer breaks that.

## Why Brython, Not JS

1. **Single-language stack.** Bernard's flow is Python end-to-end. The frontend's gatherer that reads PR data uses the same `xair.gatherers.diff` shape (conceptually) as the workflow runner.
2. **No build step.** Brython interprets the `.py` files at page load. No webpack, no tsc, no node_modules.
3. **Faster prototyping for a dashboard.** ApexCharts is JS; bair calls it via Brython's JS interop (`from browser import window; window.ApexCharts.new(...)`).

## What Lives Where

```
bair/frontend/
├── index.html             # main dashboard entry
├── observability.html     # observability snapshot view
├── py/                    # Brython modules (browser-side)
│   ├── app.py             # entry, mounts the dashboard
│   ├── api.py             # fetches from the BAIR backend
│   ├── auth.py            # token handling
│   ├── bento.py           # bento-grid layout
│   ├── chat.py            # chat UI
│   ├── config.py          # constants (NOT secrets)
│   ├── dispatch.py        # local event routing
│   ├── filters.py
│   ├── mock.py            # mock data for offline dev
│   ├── observability.py   # observability page
│   ├── pulls.py           # PR list
│   ├── runs.py            # run history
│   ├── start_work.py      # session bootstrap
│   ├── state.py           # client state mgmt
│   ├── stats.py
│   ├── table.py
│   └── ux.py              # UX helpers
├── css/styles.css
├── img/bair-logo.png      # the BAIR logo (morado neural network)
├── js/particles.js        # particle background (third-party JS, that's OK)
├── data/                  # mock JSON for offline / dev
├── diagrams/              # static HTML mermaid diagrams
├── favicon.ico
└── local_secret_helper.py  # NOT a Brython file — runs locally for dev
```

## Brython ↔ JS Interop

Calling a JS lib from Brython:

```python
# bair/frontend/py/stats.py
from browser import window, document

ApexCharts = window.ApexCharts

chart = ApexCharts.new(
    document['chart-container'],
    {
        'chart': {'type': 'line'},
        'series': [{'data': [10, 20, 30]}],
    },
)
chart.render()
```

`browser.window` and `browser.document` are Brython's bridges to the DOM. No `import requests` (that's a server-side lib); use `browser.ajax` or `browser.fetch` for HTTP.

## What's Allowed To Be JS

- Third-party libs already in JS (ApexCharts, particles.js) — fine to load via `<script src=…>` and call from Brython
- One-off inline `<script>` blocks for tiny DOM glue when Brython interop is awkward — keep them small, no logic

## What's NOT Allowed

- ❌ Re-writing `py/*.py` files in TypeScript / React / Svelte / etc.
- ❌ Adding a `package.json` + bundler to the `frontend/` dir (the dashboard is statically servable)
- ❌ Inventing a JS state-management layer when `py/state.py` already exists

## Future: If Brython Doesn't Scale

If the dashboard grows beyond what Brython can serve performant (page load >3s, charts laggy), the right move is to move logic to the SERVER (a FastAPI endpoint that returns rendered HTML or JSON for ApexCharts), NOT to rewrite to a JS framework. The single-language posture stays.
