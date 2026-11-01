"""``evo-agents hub account-pool list|show|create|delete|add|remove`` and ``hub agent account-pool HANDLE POOL``: the
account pools of a project (``evo_agents.hub.server.account_pools``), named sets of accounts that an agent in pool mode
leases from alone. Every command needs the admin role on the project or on the hub.

``add POOL NAME`` puts one of your own env or provider accounts in the pool, or with ``--agent HANDLE`` an account of
an agent of the project; ``remove POOL NAME`` takes it out (the account itself stays). ``agent account-pool HANDLE POOL``
attaches a pool to an agent, ``--none`` detaches it. Nothing prints a value. Standard library only.
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub.cli_agents import PROJECT_HELP, project_of
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_array, returns_object

EXIT_USAGE = 2
POOL_KEYS = ("id", "project", "name", "created_by", "created_at", "accounts", "agents")


def _path(project: str, pool: str | None = None) -> str:
    base = f"/v1/projects/{quote(project, safe='')}/account-pools"
    return base if pool is None else f"{base}/{quote(pool, safe='')}"


def _show(pool: dict) -> None:
    agents = ", ".join(f"@{handle}" for handle in pool["agents"]) or "none"
    print(f"{pool['name']} of project {pool['project']}: used by {agents}")
    rows = [
        (
            a["name"],
            a["kind"],
            a["env_var"] or "-",
            a["provider"],
            f"@{a['agent']}" if a["agent"] else a["owner"] or "-",
            "paused" if a["paused"] else ("cooling" if a["cooldown_until"] else "ready"),
        )
        for a in pool["accounts"]
    ]
    if rows:
        print(table(["ACCOUNT", "KIND", "ENV VAR", "PROVIDER", "OWNER", "STATE"], rows))
    else:
        print("  no account yet; `hub account-pool add` puts one in.")


def _done(args, pool: dict) -> int:
    if args.json:
        print_json(pool)
    else:
        _show(pool)
    return 0


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(project_of(args)))
    if args.json:
        print_json(found)
    elif not found:
        print("No account pool yet; `hub account-pool create NAME` makes one.")
    else:
        for pool in found:
            _show(pool)
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    return _done(args, hub.call("GET", _path(project_of(args), args.pool)))


@client_command
def cmd_create(args) -> int:
    hub, _ = signed_in()
    return _done(args, hub.call("POST", _path(project_of(args)), {"name": args.pool}))


@client_command
def cmd_delete(args) -> int:
    hub, _ = signed_in()
    hub.call("DELETE", _path(project_of(args), args.pool))
    print(f"Deleted account pool {args.pool}: its accounts stay where they are.")
    return 0


@client_command
def cmd_add(args) -> int:
    hub, _ = signed_in()
    body = {"name": args.name} | ({"agent": args.agent.removeprefix("@")} if args.agent else {})
    return _done(args, hub.call("PUT", _path(project_of(args), args.pool) + "/accounts", body))


@client_command
def cmd_remove(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    pool = hub.call("GET", _path(project, args.pool))
    agent = args.agent.removeprefix("@") if args.agent else None
    found = [a for a in pool["accounts"] if a["name"] == args.name and a["agent"] == agent]
    if not found:
        print(f"error: pool {args.pool} holds no account {args.name}", file=sys.stderr)
        return EXIT_USAGE
    return _done(args, hub.call("DELETE", _path(project, args.pool) + f"/accounts/{found[0]['id']}"))


@client_command
def cmd_attach(args) -> int:
    if (args.pool is None) == (not args.none):
        print("error: give a POOL, or --none to detach", file=sys.stderr)
        return EXIT_USAGE
    hub, _ = signed_in()
    handle = quote(args.handle.removeprefix("@"), safe="")
    path = f"/v1/projects/{quote(project_of(args), safe='')}/agents/{handle}/account-pool"
    agent = hub.call("PUT", path, {"account_pool": args.pool})
    if args.json:
        print_json(agent)
    else:
        print(f"@{agent['handle']} of project {agent['project']} uses account pool {agent['account_pool'] or 'none'}.")
    return 0


def _pool(parser) -> None:
    parser.add_argument("pool", metavar="POOL", help="the pool's name")
    parser.add_argument("--project", help=PROJECT_HELP)


def register_account_pools(hsub) -> None:
    """``account-pool ...``, added to the subparsers ``hsub`` of ``hub``."""
    top = hsub.add_parser("account-pool", help="the account pools of a project: the accounts its pool-mode agents use")
    sub = top.add_subparsers(dest="account_pool_command", required=True)

    listed = sub.add_parser("list", help="the project's account pools with their accounts and agents")
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_array(*POOL_KEYS, schema="AccountPool"))
    listed.set_defaults(func=cmd_list)

    for name, helptext, func in (
        ("show", "one account pool: its accounts and the agents that use it", cmd_show),
        ("create", "make an empty account pool (admin of the project or hub)", cmd_create),
    ):
        parser = sub.add_parser(name, help=helptext)
        _pool(parser)
        json_option(parser, returns_object(*POOL_KEYS, schema="AccountPool"))
        parser.set_defaults(func=func)

    delete = sub.add_parser("delete", help="delete an account pool no agent uses; its accounts stay")
    _pool(delete)
    delete.set_defaults(func=cmd_delete)

    for name, helptext, func in (
        ("add", "put an account of yours, or of an agent with --agent, in the pool", cmd_add),
        ("remove", "take an account out of the pool; the account stays", cmd_remove),
    ):
        parser = sub.add_parser(name, help=helptext)
        _pool(parser)
        parser.add_argument("name", metavar="NAME", help="the account's name")
        parser.add_argument("--agent", metavar="HANDLE", help="the account is this agent's, not yours")
        json_option(parser, returns_object(*POOL_KEYS, schema="AccountPool"))
        parser.set_defaults(func=func)


def register_agent_account_pool(asub) -> None:
    """``agent account-pool HANDLE [POOL|--none]``, added to the subparsers ``asub`` of ``hub agent``."""
    from evo_agents.hub.cli_agents import AGENT_KEYS

    attach = asub.add_parser("account-pool", help="attach an account pool to an agent, or detach it (admin)")
    attach.add_argument("handle", metavar="HANDLE", help="the agent, with or without @")
    attach.add_argument("pool", metavar="POOL", nargs="?", help="the pool's name")
    attach.add_argument("--none", action="store_true", help="detach the agent's pool")
    attach.add_argument("--project", help=PROJECT_HELP)
    json_option(attach, returns_object(*AGENT_KEYS, schema="Agent"))
    attach.set_defaults(func=cmd_attach)
