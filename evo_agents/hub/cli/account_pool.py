"""``evo-agents hub account-pool list|show|create|policy|delete|add|remove`` and ``hub agent account-pool HANDLE POOL``: the
account pools of a project (``evo_agents.hub.server.account_pools``), named sets of accounts that an agent in pool mode
leases from alone. Every command needs the admin role on the project or on the hub.

``add POOL NAME`` puts one of your own env or provider accounts in the pool, or with ``--agent HANDLE`` an account of
an agent of the project; ``remove POOL NAME`` takes it out (the account itself stays). ``agent account-pool HANDLE POOL``
attaches a pool to an agent, ``--none`` detaches it. ``create`` and ``policy POOL`` set the strategy the pool's agents
pick its accounts by (``--strategy``, quota-pacing among them) and the thresholds of quota-pacing (``--hard-reserve-pct``,
``--five-hour-ceiling-pct``). Nothing prints a value. Standard library only.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`AccountPools`
is the gateway to the pools of a project and :class:`AgentPools` to the pool of an agent; :func:`policy_body` turns
the policy options into the body to write and :func:`pool_view` says a pool to a person.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, View, Views, returns_array, returns_object
from evo_agents.hub.accounts import STRATEGIES
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource
from evo_agents.hub.cli_agents import AGENT_KEYS

POOL_KEYS = (
    "id",
    "project",
    "name",
    "created_by",
    "created_at",
    "accounts",
    "agents",
    "strategy",
    "hard_reserve_pct",
    "five_hour_ceiling_pct",
)
NO_POLICY = "give --strategy, --hard-reserve-pct or --five-hour-ceiling-pct"


class AccountPools(ProjectResource):
    """The account pools of a project: /v1/projects/{p}/account-pools and the routes below it (Gateway)."""

    collection = "account-pools"

    def list(self) -> list[dict]:
        return self.call("GET")

    def get(self, pool: str) -> dict:
        return self.call("GET", pool)

    def create(self, body: dict) -> dict:
        return self.call("POST", body=body)

    def change(self, pool: str, body: dict) -> dict:
        return self.call("PATCH", pool, body=body)

    def delete(self, pool: str) -> None:
        self.call("DELETE", pool)

    def add(self, pool: str, body: dict) -> dict:
        return self.call("PUT", pool, "accounts", body=body)

    def remove(self, pool: str, account_id) -> dict:
        return self.call("DELETE", pool, "accounts", account_id)


class AgentPools(ProjectResource):
    """The account pool of an agent: PUT /v1/projects/{p}/agents/{handle}/account-pool (Gateway)."""

    collection = "agents"

    def attach(self, handle: str, pool: str | None) -> dict:
        return self.call("PUT", handle.removeprefix("@"), "account-pool", body={"account_pool": pool})


def policy_text(pool: dict) -> str:
    strategy = pool.get("strategy", "round-robin")
    if strategy != "quota-pacing":
        return strategy
    return (
        f"{strategy} (reserve {pool.get('hard_reserve_pct')}%, five-hour ceiling {pool.get('five_hour_ceiling_pct')}%)"
    )


def account_state(account: dict) -> str:
    return "paused" if account["paused"] else ("cooling" if account["cooldown_until"] else "ready")


def pool_view(pool: dict) -> View:
    """What a person reads of a pool: who uses it, its strategy and its accounts."""
    agents = ", ".join(f"@{handle}" for handle in pool["agents"]) or "none"
    head = Text(f"{pool['name']} of project {pool['project']}: used by {agents}", f"  strategy {policy_text(pool)}")
    rows = [
        (
            a["name"],
            a["kind"],
            a["env_var"] or "-",
            a["provider"],
            f"@{a['agent']}" if a["agent"] else a["owner"] or "-",
            account_state(a),
        )
        for a in pool["accounts"]
    ]
    if not rows:
        return Views(head, Text("  no account yet; `hub account-pool add` puts one in."))
    # The table is followed by a line "None", as the command has always printed it (it printed what table() returned).
    return Views(head, Table(("ACCOUNT", "KIND", "ENV VAR", "PROVIDER", "OWNER", "STATE"), rows), Text("None"))


def pool_result(pool: dict) -> Result:
    return Result(pool, pool_view(pool))


def policy_body(args: argparse.Namespace) -> dict:
    """The policy options given, as the body of a pool."""
    values = {
        "strategy": args.strategy,
        "hard_reserve_pct": args.hard_reserve_pct,
        "five_hour_ceiling_pct": args.five_hour_ceiling_pct,
    }
    return {key: value for key, value in values.items() if value is not None}


POOL = Arg("pool", metavar="POOL", help="the pool's name")
POLICY = (
    Arg("--strategy", choices=STRATEGIES, help="how its agents pick among its accounts"),
    Arg(
        "--hard-reserve-pct",
        type=int,
        metavar="PCT",
        help="quota-pacing: the share of a weekly window kept back while another account has more (0 to 100)",
    ),
    Arg(
        "--five-hour-ceiling-pct",
        type=int,
        metavar="PCT",
        help="quota-pacing: the five-hour use above which an account waits while a cooler one is ready (1 to 100)",
    ),
)
ACCOUNT = (
    Arg("name", metavar="NAME", help="the account's name"),
    Arg("--agent", metavar="HANDLE", help="the account is this agent's, not yours"),
)
ONE_POOL = returns_object(*POOL_KEYS, schema="AccountPool")


class PoolList(HubCommand):
    name = "list"
    help = "the project's account pools with their accounts and agents"
    arguments = (PROJECT,)
    output = returns_array(*POOL_KEYS, schema="AccountPool")

    def run(self, ctx: HubContext) -> Result:
        found = AccountPools.of(ctx).list()
        if not found:
            return Result(found, Text("No account pool yet; `hub account-pool create NAME` makes one."))
        return Result(found, Views(*(pool_view(pool) for pool in found)))


class PoolShow(HubCommand):
    name = "show"
    help = "one account pool: its accounts and the agents that use it"
    arguments = (POOL, PROJECT)
    output = ONE_POOL

    def run(self, ctx: HubContext) -> Result:
        return pool_result(AccountPools.of(ctx).get(ctx.args.pool) or {})


class PoolCreate(HubCommand):
    name = "create"
    help = "make an empty account pool (admin of the project or hub)"
    arguments = (POOL, PROJECT, *POLICY)
    output = ONE_POOL

    def run(self, ctx: HubContext) -> Result:
        return pool_result(AccountPools.of(ctx).create({"name": ctx.args.pool} | policy_body(ctx.args)) or {})


class PoolPolicy(HubCommand):
    name = "policy"
    help = "set the strategy and quota-pacing thresholds of a pool (admin)"
    arguments = (POOL, PROJECT, *POLICY)
    output = ONE_POOL

    def check(self, args: argparse.Namespace) -> None:
        if not policy_body(args):
            raise UsageError(NO_POLICY)

    def run(self, ctx: HubContext) -> Result:
        return pool_result(AccountPools.of(ctx).change(ctx.args.pool, policy_body(ctx.args)) or {})


class PoolDelete(HubCommand):
    name = "delete"
    help = "delete an account pool no agent uses; its accounts stay"
    arguments = (POOL, PROJECT)

    def run(self, ctx: HubContext) -> Result:
        AccountPools.of(ctx).delete(ctx.args.pool)
        return Result(None, Text(f"Deleted account pool {ctx.args.pool}: its accounts stay where they are."))


class PoolAdd(HubCommand):
    name = "add"
    help = "put an account of yours, or of an agent with --agent, in the pool"
    arguments = (POOL, PROJECT, *ACCOUNT)
    output = ONE_POOL

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"name": args.name} | ({"agent": args.agent.removeprefix("@")} if args.agent else {})
        return pool_result(AccountPools.of(ctx).add(args.pool, body))


class PoolRemove(HubCommand):
    name = "remove"
    help = "take an account out of the pool; the account stays"
    arguments = (POOL, PROJECT, *ACCOUNT)
    output = ONE_POOL

    def run(self, ctx: HubContext) -> Result:
        args, pools = ctx.args, AccountPools.of(ctx)
        agent = args.agent.removeprefix("@") if args.agent else None
        found = [a for a in pools.get(args.pool)["accounts"] if a["name"] == args.name and a["agent"] == agent]
        if not found:
            raise UsageError(f"pool {args.pool} holds no account {args.name}")
        return pool_result(pools.remove(args.pool, found[0]["id"]))


class AgentAccountPool(HubCommand):
    name = "account-pool"
    help = "attach an account pool to an agent, or detach it (admin)"
    arguments = (
        Arg("handle", metavar="HANDLE", help="the agent, with or without @"),
        Arg("pool", metavar="POOL", nargs="?", help="the pool's name"),
        Arg("--none", action="store_true", help="detach the agent's pool"),
        PROJECT,
    )
    output = returns_object(*AGENT_KEYS, schema="Agent")

    def check(self, args: argparse.Namespace) -> None:
        if (args.pool is None) == (not args.none):
            raise UsageError("give a POOL, or --none to detach")

    def run(self, ctx: HubContext) -> Result:
        agent = AgentPools.of(ctx).attach(ctx.args.handle, ctx.args.pool)
        said = f"@{agent['handle']} of project {agent['project']} uses account pool {agent['account_pool'] or 'none'}."
        return Result(agent, Text(said))


ACCOUNT_POOL = Group(  # hub account-pool
    "account-pool",
    "the account pools of a project: the accounts its pool-mode agents use",
    [PoolList(), PoolShow(), PoolCreate(), PoolPolicy(), PoolDelete(), PoolAdd(), PoolRemove()],
)
AGENT_ACCOUNT_POOL = AgentAccountPool()  # hub agent account-pool
