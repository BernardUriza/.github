"""Entry point: `python -m bair <command>` dispatches through xair.

The xair runtime owns the command registry. bair only registers its own
pipelines (in :mod:`bair.pipelines`) at import time, then hands control to
xair's dispatcher."""

from __future__ import annotations

import sys

# Importing :mod:`bair.pipelines` triggers @command registration via xair.
from . import pipelines  # noqa: F401  # pyright: ignore[reportUnusedImport]


def main(argv: list[str] | None = None) -> int:
    from xair.dispatch import dispatch

    return dispatch(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
