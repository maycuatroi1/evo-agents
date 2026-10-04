"""The read rule of the hub's web pages: a member reads with the label their grant reaches, not through a sink.

The web shows a project's content to the member who is signed in, in their own browser. The sink rule of
``evo_agents.hub.access`` (the clearance of the runtime that receives the content, ``X-Evo-Sink`` on /mcp) does not
apply there; the grant's max level does, alone. ``grant_ceiling`` is that label, on the same lattice and with the
same grant label ``ProjectRules.ceiling`` meets with a sink's clearance, so for any sink
``ceiling(max_level, sink) == grant_ceiling(max_level).meet(clearance of sink)``. It fails closed the same way: no
grant, or a max level the ladder lacks, lets nothing through.

Kept in a module of its own so the grant-only rule of the other web pages can be merged into this one function.
"""

from __future__ import annotations

from evo_agents.hub.access import UNTRUSTED, ProjectRules
from evo_agents.kg.policy import Label


def grant_ceiling(rules: ProjectRules, max_level: str | None) -> Label | None:
    """The highest label a member whose grant reaches ``max_level`` may read on the web; None when nothing passes."""
    if not isinstance(max_level, str) or max_level not in rules.levels:
        return None
    return Label(rules.levels.index(max_level), len(rules.locations) - 1, UNTRUSTED, frozenset({rules.name}))
