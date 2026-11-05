"""``evo-agents hub admin kg-pool``: the shared KG pool of the hub (``evo_agents.hub.server.kg_shared_pool_api``), a hub
admin's.

With no option it prints the pool named now, its owner and the projects it serves. ``--set OWNER/POOL`` names that
member's pool, in place of the one named before; ``--clear`` names none. Its ready nodes, which run evo-agents with the
feature repoless_review, take the KG runs (lens kg_questions and kg_init) of a project with no repo whose default
agent's pool has no ready node, when the project's creator owns the pool; the hub adds each such project to the pool.
Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.contract import json_option, returns_object

VIEW_KEYS = ("pool", "owner", "projects")
PATH = "/v1/admin/kg-pool"


@client_command
def cmd_kg_pool(args) -> int:
    if args.set and args.clear:
        print("error: pass --set OWNER/POOL or --clear, not both", file=sys.stderr)
        return 2
    if args.set and args.set.count("/") != 1:
        print(f"error: --set takes OWNER/POOL, the owner's login and the pool's name, not {args.set}", file=sys.stderr)
        return 2
    hub, _ = signed_in()
    if args.set:
        owner, pool = args.set.split("/")
        view = hub.call("PUT", PATH, {"owner": owner, "pool": pool}) or {}
    else:
        view = hub.call("DELETE" if args.clear else "GET", PATH) or {}
    if args.json:
        print_json(view)
    elif view["pool"] is None:
        print("No shared KG pool: the KG runs of a project with no repo wait for a node of its agent's pool.")
    else:
        served = ", ".join(view["projects"]) or "none yet"
        print(f"Shared KG pool: {view['owner']}/{view['pool']}; projects it serves: {served}.")
    return 0


def kg_pool_commands(asub) -> None:
    """Add ``kg-pool`` to the ``hub admin`` subparsers ``asub``."""
    kg_pool = asub.add_parser(
        "kg-pool", help="the pool whose nodes take the KG runs of projects with no repo (hub admins only)"
    )
    kg_pool.add_argument("--set", metavar="OWNER/POOL", help="name this member's pool the shared KG pool")
    kg_pool.add_argument("--clear", action="store_true", help="name no shared KG pool")
    json_option(kg_pool, returns_object(*VIEW_KEYS, schema="KgSharedPool"))
    kg_pool.set_defaults(func=cmd_kg_pool)
