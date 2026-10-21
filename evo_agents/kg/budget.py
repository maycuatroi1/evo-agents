"""The budget of kg_context: its whole text, nodes and edges, stays within ``budget_tokens`` estimated as UTF-8 bytes
divided by 3. ``Room`` counts the bytes of the lines it takes and what it reached that did not fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONTEXT_QUERY_SHOWN = 100  # characters of the query kg_context's first line repeats
CONTEXT_NOTE_BYTES = 200  # kept for the line that says what kg_context left out
CONTEXT_LABEL_BYTES = 120  # kept for the line that prints the label most nodes share
CONTEXT_MIN_LINE = 40  # below this much room no edge line fits: kg_context stops reading


def line_size(line: str) -> int:
    """What a line of text costs in a budget: its UTF-8 bytes and the newline after it."""
    return len(line.encode("utf-8")) + 1


@dataclass
class Room:
    """The bytes kg_context's node and edge lines may take, the nodes and edges it put in them, and those it reached
    that did not fit. ``beyond`` is true when it stopped with nodes still to expand."""

    size: int
    used: int = 0
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: dict[str, dict] = field(default_factory=dict)
    left_nodes: set[str] = field(default_factory=set)
    left_edges: set[str] = field(default_factory=set)
    beyond: bool = False

    def take(self, cost: int) -> bool:
        if self.used + cost > self.size:
            return False
        self.used += cost
        return True

    def take_node(self, node_id: str, node: dict, cost: int) -> bool:
        if not self.take(cost):
            return False
        self.nodes[node_id] = node
        return True

    def left(self) -> tuple[int, int]:
        return len(self.left_nodes - set(self.nodes)), len(self.left_edges - set(self.edges))

    def note(self) -> list[str]:
        nodes, edges = self.left()
        if not (nodes or edges or self.beyond):
            return []
        beyond = ", and the nodes further out" if self.beyond else ""
        return [
            f"left out by the budget: {nodes} node(s) and {edges} edge(s) it reached{beyond}; raise budget_tokens (up "
            "to 8000) or pass ids"
        ]
