"""The worker extra (``evo-ak[worker]``): whether this Python has it, and the line that says how to install it, for the
commands that need it (``cli.base``, ``service``) and the ones that load it themselves. Standard library only."""

from __future__ import annotations

import importlib.util

EXTRA_HINT = "uv tool install 'evo-ak[worker]' (or python -m pip install 'evo-ak[worker]')"


def missing_extra() -> str | None:
    """The module of the worker extra that ``worker run`` needs and this Python lacks, or None."""
    return None if importlib.util.find_spec("aiohttp") is not None else "aiohttp"
