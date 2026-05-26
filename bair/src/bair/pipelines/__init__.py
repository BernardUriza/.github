"""bair pipelines — concrete commands wired against Bernard's ecosystem.

Importing this package triggers side-effect ``@command`` registrations
for every pipeline below. The dispatcher in xair looks up commands in
the registry by name; without these imports the registry stays empty
and ``python -m bair <cmd>`` returns "unknown command".

Add a new pipeline:

  1. Drop ``<name>.py`` next to this file.
  2. Decorate the handler with ``@command("<name>")`` from xair.command_registry.
  3. Append ``from . import <name>  # noqa: F401`` below.
"""

from . import gatekeep  # noqa: F401  # pyright: ignore[reportUnusedImport]
