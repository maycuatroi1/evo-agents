"""The constants of the documents a plugin saves (``routes.docs``)."""

from __future__ import annotations

PERMISSION = "docs:write"
FEATURE = "docs"
RACE = "another save of the document took the same version number at the same moment: nothing was written; try again"


def slug_prefix(plugin: str) -> str:
    """What the slug of every document plugin ``plugin`` saves starts with."""
    return f"{plugin}-"


__all__ = ["FEATURE", "PERMISSION", "RACE", "slug_prefix"]
