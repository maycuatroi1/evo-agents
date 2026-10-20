"""What a built graph says of how far its call edges reach, for the hub's measure of a graph's health
(``evo_agents.hub.kg_coverage``): the Python functions and methods outside tests, and how many of them no live
``calls`` edge leads into. A method that nothing calls in the graph is one ``kg_impact`` finds no caller of, whether
none exists or the extractor missed the call; the share of them is what the review of 2026-10-17 measured (59% of
the methods, against 20% of the functions). It only reads the store.
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath

from evo_agents.kg.store import Store

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


def call_reach(store: Store, b: int) -> dict:
    """The Python symbols of kinds KINDS that build ``b`` holds outside tests, by kind: how many there are, how many
    no live ``calls`` edge leads into (``uncalled``), and the share of them (0 when there is none)."""
    rows = store.db.execute(
        "SELECT json_extract(n.props, '$.kind'), json_extract(n.props, '$.path'),"
        " EXISTS (SELECT 1 FROM edges e WHERE e.dst = n.node_id AND e.rel = 'calls'"
        " AND e.tx_from <= :b AND (e.tx_to IS NULL OR e.tx_to > :b))"
        " FROM nodes n WHERE n.kind = 'Symbol' AND n.tx_from <= :b AND (n.tx_to IS NULL OR n.tx_to > :b)"
        " AND json_extract(n.props, '$.path') LIKE '%.py'",
        {"b": b},
    )
    total, uncalled = Counter(), Counter()
    for kind, path, called in rows:
        if kind in KINDS and isinstance(path, str) and not is_test_path(path):
            total[kind] += 1
            uncalled[kind] += 0 if called else 1
    out = {}
    for kind in KINDS:
        out[f"{kind}s"] = total[kind]
        out[f"uncalled_{kind}s"] = uncalled[kind]
        out[f"uncalled_{kind}_ratio"] = round(uncalled[kind] / total[kind], 4) if total[kind] else 0.0
    return out
