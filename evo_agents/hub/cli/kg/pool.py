"""``evo-agents hub admin kg-pool``: the shared KG pool of the hub (``evo_agents.hub.server.kg_shared_pool_api``), a hub
admin's.

With no option it prints the pool named now, its owner and the projects it serves. ``--set OWNER/POOL`` names that
member's pool, in place of the one named before; ``--clear`` names none. Its ready nodes, which run evo-agents with the
feature repoless_review, take the KG runs (lens kg_questions and kg_init) of a project with no repo whose default
agent's pool has no ready node, when the project's creator owns the pool; the hub adds each such project to the pool.
The command is :data:`KG_POOL`, which ``hub admin`` lists (``evo_agents.hub.cli_invites``). Standard library only, like
the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text, UsageError, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.kg_routes import KgAdmin

__all__ = ["KG_POOL"]

VIEW_KEYS = ("pool", "owner", "projects")


class KgPool(HubCommand):
    name = "kg-pool"
    help = "the pool whose nodes take the KG runs of projects with no repo (hub admins only)"
    arguments = (
        Arg("--set", metavar="OWNER/POOL", help="name this member's pool the shared KG pool"),
        Arg("--clear", action="store_true", help="name no shared KG pool"),
    )
    output = returns_object(*VIEW_KEYS, schema="KgSharedPool")

    def check(self, args) -> None:
        if args.set and args.clear:
            raise UsageError("pass --set OWNER/POOL or --clear, not both")
        if args.set and args.set.count("/") != 1:
            raise UsageError(f"--set takes OWNER/POOL, the owner's login and the pool's name, not {args.set}")

    def run(self, ctx: HubContext) -> Result:
        admin = KgAdmin(ctx.hub)
        if ctx.args.set:
            owner, pool = ctx.args.set.split("/")
            view = admin.set_pool(owner, pool)
        else:
            view = admin.clear_pool() if ctx.args.clear else admin.pool()
        if view["pool"] is None:
            return Result(
                view,
                Text("No shared KG pool: the KG runs of a project with no repo wait for a node of its agent's pool."),
            )
        served = ", ".join(view["projects"]) or "none yet"
        return Result(view, Text(f"Shared KG pool: {view['owner']}/{view['pool']}; projects it serves: {served}."))


KG_POOL = KgPool()
