"""What a built graph says of how far its call edges reach, for the hub's measure of a graph's health
(``evo_agents.hub.kg_coverage``): the Python functions and methods outside tests, and how many of them no live
``calls`` edge leads into. A method that nothing calls in the graph is one ``kg_impact`` finds no caller of, whether
none exists or the extractor missed the call; the share of them is what the review of 2026-10-17 measured (59% of
the methods, against 20% of the functions). The count itself is ``evo_agents.kg.graph_rows.call_reach``, over the rows
of a build; this module holds what it counts by.
"""

from __future__ import annotations

from pathlib import PurePosixPath

KINDS = ("function", "method")
TEST_DIRS = frozenset({"test", "tests"})


def is_test_path(path: str) -> bool:
    """Whether ``path`` is a test's: under a ``test`` or ``tests`` directory, ``test_*.py``, ``*_test.py`` or
    ``conftest.py``."""
    pure = PurePosixPath(path)
    name = pure.name
    return (
        bool(TEST_DIRS & set(pure.parts[:-1]))
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )
