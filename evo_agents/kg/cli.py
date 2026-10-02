"""``evo-agents kg`` subcommands."""

from __future__ import annotations


def register(sub) -> None:
    kg = sub.add_parser("kg", help="multi-source knowledge graph")
    kg.add_subparsers(dest="command", required=True)
