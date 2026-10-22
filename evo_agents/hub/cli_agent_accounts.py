"""``evo-agents hub agent account list|show|add|renew|set|pause|resume|remove``: the accounts of an agent, env secrets its
profile owns (``evo_agents.hub.server.agent_accounts``), over PUT, PATCH and DELETE
/v1/projects/{p}/agents/{handle}/accounts/{name}. Writing needs the admin role on the project.

``list`` and ``show`` need any grant on the project (``evo_agents.hub.server.agent_account_views``): ``list`` reads GET
/v1/agent-accounts, the accounts of the agents of every project you hold a grant on, narrowed to one agent with HANDLE
(in ``--project`` or the harness's project) or to one project with ``--project``; ``show HANDLE NAME`` reads one account.
Both give each account's provider, state, workers, weight, priority, end, last usage, runs and their cost over 24 hours
and 7 days (none for a service account) and the runs holding it; never a value.

``add`` gives the agent the account NAME for ``--env-var``, bound to each ``--worker`` (yours that are not revoked, or a
member's shared worker, each serving the project); it replaces the account of that name whole, keeping its id.
``renew`` writes a new value for the same account and changes nothing else (PATCH ``value``). ``set`` changes
``--weight``, ``--priority``, the ``--worker`` list, the end (``--expires`` or ``--no-expiry``); ``pause`` and ``resume``
keep new runs off an account or let them take it again; ``remove`` deletes it, its value and bindings going from the hub
and its leases still out being revoked.

The value never comes from the command line: it is read from stdin when stdin is not a terminal, else asked for
without echo (``cli_secrets.read_value``), checked for what the hub refuses, and never printed, not even in an error.
Every check of the arguments comes before the value is read. ``--expires YYYY-MM-DD`` ends the account at 00:00 UTC of
that day. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from urllib.parse import quote, urlencode

from evo_agents.hub.accounts import MAX_PRIORITY, MAX_WEIGHT
from evo_agents.hub.cli_agents import PROJECT_HELP, project_of
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.cli_secrets import Refused, check_name, check_value, expires_at, read_value
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.credentials import env_name_refusal
from evo_agents.hub.curator import money

EXIT_USAGE = 2
# The keys of an account of an agent in --json (the contract, `evo-agents hub contract print`).
ACCOUNT_KEYS = (
    "id",
    "name",
    "env_var",
    "workers",
    "weight",
    "priority",
    "paused_at",
    "cooldown_until",
    "cooldown_reason",
    "last_leased_at",
    "expires_at",
    "state",
    "created_at",
    "updated_at",
    "provider",
    "category",
    "last_usage",
    "last_usage_at",
    "runs_24h",
    "runs_7d",
    "cost_24h_usd",
    "cost_7d_usd",
    "held_by",
)
# The keys of an account of `agent account list --json`: an account with its project and agent.
LISTED_KEYS = (*ACCOUNT_KEYS, "project", "handle", "agent_name", "color", "icon_url")


def _path(project: str, handle: str, name: str) -> str:
    agent = quote(handle.removeprefix("@"), safe="")
    return f"/v1/projects/{quote(project, safe='')}/agents/{agent}/accounts/{quote(name, safe='')}"


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _line(handle: str, account: dict, verb: str) -> str:
    ends = f"; it ends {when(account['expires_at'])} UTC" if account["expires_at"] else ""
    workers = ", ".join(account["workers"])
    return (
        f"{verb} account {account['name']} ({account['env_var']}) of @{handle.removeprefix('@')}: {account['state']}, "
        f"weight {account['weight']}, priority {account['priority']}, on {workers}{ends}."
    )


def _bounds(args) -> str | None:
    for key, top in (("weight", MAX_WEIGHT), ("priority", MAX_PRIORITY)):
        given = getattr(args, key, None)
        if given is not None and not 0 <= given <= top:
            return f"--{key} is 0 to {top}, not {given}"
    return None


def _done(args, account: dict, verb: str) -> int:
    if args.json:
        print_json(account)
    else:
        print(_line(args.handle, account, verb))
    return 0


def _checked(args) -> str | None:
    """The refusal of the arguments every command shares: the account's name, weight and priority."""
    try:
        check_name(args.name)
    except Refused as exc:
        return str(exc)
    return _bounds(args)


def _read(args) -> str:
    value = read_value(
        args.name, prompt=f"Value of account {args.name} of @{args.handle.removeprefix('@')} (not shown): "
    )
    check_value("env", value)
    return value


@client_command
def cmd_add(args) -> int:
    refusal = _checked(args) or env_name_refusal(args.env_var)
    try:
        if refusal is None and args.expires is not None:
            args.expires = expires_at(args.expires)
    except Refused as exc:
        refusal = str(exc)
    if refusal is not None:
        return _usage(refusal)
    hub, _ = signed_in()  # before the value is asked for: not signed in costs no paste
    try:
        value = _read(args)
    except Refused as exc:
        return _usage(str(exc))
    body = {"env_var": args.env_var, "workers": args.worker, "value": value}
    body |= {key: getattr(args, key) for key in ("weight", "priority") if getattr(args, key) is not None}
    if args.expires is not None:
        body["expires_at"] = args.expires
    written = hub.call("PUT", _path(project_of(args), args.handle, args.name), body)
    return _done(args, written, "Created" if written["created"] else "Replaced")


@client_command
def cmd_renew(args) -> int:
    if (refusal := _checked(args)) is not None:
        return _usage(refusal)
    hub, _ = signed_in()
    try:
        value = _read(args)
    except Refused as exc:
        return _usage(str(exc))
    body = {"value": value}
    if args.expires is not None:
        try:
            body["expires_at"] = expires_at(args.expires)
        except Refused as exc:
            return _usage(str(exc))
    renewed = hub.call("PATCH", _path(project_of(args), args.handle, args.name), body)
    return _done(args, renewed, "Renewed")


def _change(args, body: dict, verb: str) -> int:
    hub, _ = signed_in()
    changed = hub.call("PATCH", _path(project_of(args), args.handle, args.name), body)
    return _done(args, changed, verb)


@client_command
def cmd_set(args) -> int:
    if (refusal := _checked(args)) is not None:
        return _usage(refusal)
    body = {key: getattr(args, key) for key in ("weight", "priority") if getattr(args, key) is not None}
    if args.worker:
        body["workers"] = args.worker
    try:
        if args.expires is not None:
            body["expires_at"] = expires_at(args.expires)
    except Refused as exc:
        return _usage(str(exc))
    if args.no_expiry:
        body["expires_at"] = None
    if not body:
        return _usage("give something to change: --weight, --priority, --worker, --expires or --no-expiry")
    return _change(args, body, "Changed")


@client_command
def cmd_pause(args) -> int:
    if (refusal := _checked(args)) is not None:
        return _usage(refusal)
    return _change(args, {"paused": True}, "Paused")


@client_command
def cmd_resume(args) -> int:
    if (refusal := _checked(args)) is not None:
        return _usage(refusal)
    return _change(args, {"paused": False}, "Resumed")


@client_command
def cmd_remove(args) -> int:
    if (refusal := _checked(args)) is not None:
        return _usage(refusal)
    hub, _ = signed_in()
    hub.call("DELETE", _path(project_of(args), args.handle, args.name))
    print(
        f"Deleted account {args.name} of @{args.handle.removeprefix('@')}: its value and bindings are gone from the "
        "hub, and its leases still out are revoked. Revoke it where it was made as well."
    )
    return 0


# Reading


def _usage_text(usage: dict | None) -> str:
    if not usage:
        return "-"
    used = usage.get("utilization")
    return usage["status"] if used is None else f"{usage['status']} {round(used * 100)}%"


def _runs(account: dict) -> str:
    runs = f"{account['runs_24h']}/{account['runs_7d']}"
    return runs if account["cost_7d_usd"] is None else f"{runs} ({money(account['cost_7d_usd'])})"


@client_command
def cmd_list(args) -> int:
    query = {}
    if args.handle is not None:
        query = {"project": project_of(args), "handle": args.handle.removeprefix("@")}
    elif args.project:
        query = {"project": args.project}
    hub, _ = signed_in()
    found = hub.call("GET", "/v1/agent-accounts" + ("?" + urlencode(query) if query else ""))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print("No agent of a project you hold a grant on has an account; `hub agent account add` gives one.")
        return 0
    rows = [
        (
            a["project"],
            f"@{a['handle']}",
            a["name"],
            a["env_var"],
            a["provider"],
            a["state"],
            ", ".join(a["workers"]),
            f"{a['weight']}/{a['priority']}",
            _usage_text(a["last_usage"]),
            _runs(a),
            " ".join(f"#{run['run_id']}" for run in a["held_by"]) or "-",
            when(a["last_leased_at"]),
        )
        for a in found
    ]
    header = ("PROJECT", "AGENT", "NAME", "VARIABLE", "PROVIDER", "STATE", "WORKERS", "WEIGHT/PRIO", "USAGE")
    table((*header, "RUNS 24H/7D", "HELD BY", "LAST USED"), rows)
    return 0


@client_command
def cmd_show(args) -> int:
    if (refusal := _checked(args)) is not None:
        return _usage(refusal)
    hub, _ = signed_in()
    a = hub.call("GET", _path(project_of(args), args.handle, args.name))
    if args.json:
        print_json(a)
        return 0
    held = ", ".join(f"run #{run['run_id']} ({run['project']}, {run['state']})" for run in a["held_by"]) or "no run"
    reported = f", reported {when(a['last_usage_at'])} UTC" if a["last_usage_at"] else ""
    print(f"Account {a['name']} ({a['env_var']}, {a['provider']}) of @{args.handle.removeprefix('@')}: {a['state']}.")
    print(f"  Workers: {', '.join(a['workers'])}; expires: {when(a['expires_at']) if a['expires_at'] else 'never'}.")
    print(f"  Weight {a['weight']}, priority {a['priority']}; last taken {when(a['last_leased_at'])} UTC.")
    print(f"  Last usage: {_usage_text(a['last_usage'])}{reported}.")
    if a["cost_24h_usd"] is None:  # a service account: the cost of a run is its model account's
        print(f"  Runs: {a['runs_24h']} in 24 hours, {a['runs_7d']} in 7 days.")
    else:
        print(f"  Runs: {a['runs_24h']} in 24 hours ({money(a['cost_24h_usd'])}), {a['runs_7d']} in 7 days", end="")
        print(f" ({money(a['cost_7d_usd'])}, estimated from their usage).")
    print(f"  Held by: {held}.")
    return 0


# Registering


def _named(sub, command: str, help: str, func, schema: str = "AgentAccount", added: tuple[str, ...] = ()):
    parser = sub.add_parser(command, help=help)
    parser.add_argument("handle", metavar="HANDLE", help="the agent's handle, with or without @")
    parser.add_argument("name", metavar="NAME", help="the account's name, unique among the agent's accounts")
    parser.add_argument("--project", help=PROJECT_HELP)
    if command != "remove":
        json_option(parser, returns_object(*ACCOUNT_KEYS, *added, schema=schema))
    else:
        parser.set_defaults(json=False)
    parser.set_defaults(func=func)
    return parser


def _weights(parser) -> None:
    parser.add_argument("--weight", type=int, metavar="N", help=f"0 to {MAX_WEIGHT}: its share in a weighted pool")
    parser.add_argument("--priority", type=int, metavar="N", help=f"0 to {MAX_PRIORITY}: the lower, the sooner")


def register_agent_accounts(asub) -> None:
    """``agent account ...``, added to the subparsers ``asub`` of ``hub agent``."""
    account = asub.add_parser(
        "account", help="the accounts of an agent: env secrets its runs take on the workers bound"
    )
    csub = account.add_subparsers(dest="account_command", required=True)

    listed = csub.add_parser(
        "list", help="the accounts of the agents of your projects, or of one agent: state, usage, runs, never a value"
    )
    listed.add_argument("handle", metavar="HANDLE", nargs="?", help="only this agent's, with or without @")
    listed.add_argument("--project", help="only this project's agents; with HANDLE, " + PROJECT_HELP)
    json_option(listed, returns_array(*LISTED_KEYS, schema="ListedAgentAccount"))
    listed.set_defaults(func=cmd_list)
    _named(csub, "show", "one account of an agent: state, last usage, runs and cost, runs holding it", cmd_show)

    add = _named(
        csub,
        "add",
        "give an agent an account, or replace the one of that name (the value comes from stdin or a prompt)",
        cmd_add,
        "AgentAccountWritten",
        ("created",),
    )
    add.add_argument("--env-var", required=True, metavar="VAR", help="the variable it sets in the agent's runs")
    add.add_argument(
        "--worker", action="append", required=True, metavar="NAME", help="a worker it is bound to; repeat it"
    )
    add.add_argument("--expires", metavar="YYYY-MM-DD", help="the day it ends, 00:00 UTC")
    _weights(add)

    renew = _named(csub, "renew", "write a new value for an account, which stays the same account", cmd_renew)
    renew.add_argument("--expires", metavar="YYYY-MM-DD", help="its new end, 00:00 UTC")

    changed = _named(csub, "set", "change the weight, priority, workers or end of an account", cmd_set)
    _weights(changed)
    changed.add_argument("--worker", action="append", metavar="NAME", help="the workers it is bound to; repeat it")
    ends = changed.add_mutually_exclusive_group()
    ends.add_argument("--expires", metavar="YYYY-MM-DD", help="its new end, 00:00 UTC")
    ends.add_argument("--no-expiry", action="store_true", help="drop its end")

    _named(csub, "pause", "keep new runs off an account; a run that holds it keeps it", cmd_pause)
    _named(csub, "resume", "let new runs take a paused account again", cmd_resume)
    _named(csub, "remove", "delete an account: its value and bindings go, its leases out are revoked", cmd_remove)
