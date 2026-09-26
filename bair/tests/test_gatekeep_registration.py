"""The `gatekeep` command must dispatch to the pipeline, not to a helper.

Por qué existe (2026-09-26): al extraer `review()` quedó insertado entre el
`@command("gatekeep")` y `def gatekeep`, así que xair registró `review` como el
handler y cada PR de server-bot tronó con `review() missing 2 required positional
arguments` (run 36209345048). Ningún test pasaba por el registry.
"""

from __future__ import annotations

import inspect

from xair.command_registry import get_handler

import bair.pipelines  # noqa: F401  # side-effect registration, as `python -m bair` does
from bair.pipelines import gatekeep


def test_the_gatekeep_command_is_the_pipeline_entrypoint():
    handler = get_handler("gatekeep")
    assert handler is gatekeep.gatekeep
    assert list(inspect.signature(handler).parameters) == ["ctx", "container"]
