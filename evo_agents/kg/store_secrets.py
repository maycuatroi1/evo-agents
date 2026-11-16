"""The rows of a graph file holding a secret (``evo_agents.kg.scrub.row_secrets``): what the hub's scrub reads in the
graph files older builds uploaded, until they leave the bucket (plan kg-centralized-on-hub, step 7). It goes with the
graph store's file."""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

from evo_agents.kg.scrub import row_secrets

GRAPH_QUERIES = (  # every row of the graph that holds text, current or not
    "SELECT name, body FROM node_fts",
    "SELECT name, props FROM nodes",
    "SELECT props, NULL FROM edges",
)


def graph_secrets(path: Path, *, immutable: bool = False) -> tuple[int, dict[str, int]]:
    """The rows of the graph file at ``path`` holding a secret, and the secrets by kind: the full-text index of the
    nodes (name and text), and the name and properties of every node and edge, current or not."""
    mode = "mode=ro&immutable=1" if immutable else "mode=ro"
    db = sqlite3.connect(f"{path.resolve().as_uri()}?{mode}", uri=True)
    rows, kinds = 0, Counter()
    try:
        for query in GRAPH_QUERIES:
            try:
                found = db.execute(query)
            except sqlite3.OperationalError:  # a store from another schema: what it lacks holds nothing
                continue
            for first, second in found:
                counts = row_secrets(first, second)
                if counts:
                    rows += 1
                    kinds.update(counts)
    finally:
        db.close()
    return rows, dict(sorted(kinds.items()))
