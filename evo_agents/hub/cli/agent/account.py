"""``evo-agents hub agent account list|show|add|renew|set|pause|resume|remove``: the accounts of an agent, env secrets its
profile owns (``evo_agents.hub.server.agent_accounts``), over PUT, PATCH and DELETE
/v1/projects/{p}/agents/{handle}/accounts/{name}. Writing needs the admin role on the project.

``list`` and ``show`` need any grant on the project (``evo_agents.hub.server.agent_accounts.service.views``): ``list`` reads GET
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
without echo (``cli.secret.values.read_value``), checked for what the hub refuses, and never printed, not even in an error.
Every check of the arguments comes before the value is read. ``--expires YYYY-MM-DD`` ends the account at 00:00 UTC of
that day. Standard library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`AgentAccounts`
is the gateway to the accounts of a project's agents and :class:`ListedAccounts` to GET /v1/agent-accounts;
:func:`change_body` turns the options of ``set`` into the change to write and :func:`account_line` and
:func:`account_lines` say an account to a person.
"""

from __future__ import annotations

import argparse
from typing import Any, ClassVar
from urllib.parse import urlencode

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, returns_array, returns_object
from evo_agents.hub.accounts import MAX_PRIORITY, MAX_WEIGHT
from evo_agents.hub.cli.base import PROJECT, PROJECT_HELP, HubCommand, HubContext, HubRoutes, ProjectResource, when
from evo_agents.hub.cli.secret.values import Refused, check_name, check_value, expires_at, read_value
from evo_agents.hub.credentials import env_name_refusal
from evo_agents.hub.curator import money

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
NOTHING_TO_CHANGE = "give something to change: --weight, --priority, --worker, --expires or --no-expiry"


class AgentAccounts(ProjectResource):
    """The accounts of a project's agents: GET, PUT, PATCH and DELETE /v1/projects/{p}/agents/{handle}/accounts/{name}
    (Gateway)."""

    collection = "agents"

    def account(self, method: str, handle: str, name: str, body: dict | None = None):
        return self.call(method, handle.removeprefix("@"), "accounts", name, body=body)


class ListedAccounts(HubRoutes):
    """GET /v1/agent-accounts: the accounts of the agents of every project the member holds a grant on (Gateway)."""

    base = "agent-accounts"

    def list(self, query: dict) -> Any:
        return self.hub.call("GET", self.path + ("?" + urlencode(query) if query else ""))


def at(handle: str) -> str:
    return f"@{handle.removeprefix('@')}"


def account_line(handle: str, account: dict, verb: str) -> str:
    """What a command that writes an account prints of it."""
    ends = f"; it ends {when(account['expires_at'])} UTC" if account["expires_at"] else ""
    workers = ", ".join(account["workers"])
    return (
        f"{verb} account {account['name']} ({account['env_var']}) of {at(handle)}: {account['state']}, "
        f"weight {account['weight']}, priority {account['priority']}, on {workers}{ends}."
    )


def refusal(args: argparse.Namespace) -> str | None:
    """The refusal of the arguments every command on one account shares: its name, weight and priority."""
    try:
        check_name(args.name)
    except Refused as exc:
        return str(exc)
    for key, top in (("weight", MAX_WEIGHT), ("priority", MAX_PRIORITY)):
        given = getattr(args, key, None)
        if given is not None and not 0 <= given <= top:
            return f"--{key} is 0 to {top}, not {given}"
    return None


def checked(args: argparse.Namespace) -> None:
    if (why := refusal(args)) is not None:
        raise UsageError(why)


def day(text: str) -> str:
    """``--expires`` as the moment the account ends; UsageError when it is no day."""
    try:
        return expires_at(text)
    except Refused as exc:
        raise UsageError(str(exc)) from None


def weights(args: argparse.Namespace) -> dict:
    return {key: getattr(args, key) for key in ("weight", "priority") if getattr(args, key) is not None}


def change_body(args: argparse.Namespace) -> dict:
    """The PATCH body of ``set``; UsageError when it changes nothing or its end is no day."""
    body = weights(args)
    if args.worker:
        body["workers"] = args.worker
    if args.expires is not None:
        body["expires_at"] = day(args.expires)
    if args.no_expiry:
        body["expires_at"] = None
    if not body:
        raise UsageError(NOTHING_TO_CHANGE)
    return body


def read(args: argparse.Namespace) -> str:
    """The account's value, read and checked; UsageError, never with the value, when it cannot be one."""
    try:
        value = read_value(args.name, prompt=f"Value of account {args.name} of {at(args.handle)} (not shown): ")
        check_value("env", value)
    except Refused as exc:
        raise UsageError(str(exc)) from None
    return value


def usage_text(usage: dict | None) -> str:
    if not usage:
        return "-"
    used = usage.get("utilization")
    return usage["status"] if used is None else f"{usage['status']} {round(used * 100)}%"


def runs_text(account: dict) -> str:
    runs = f"{account['runs_24h']}/{account['runs_7d']}"
    return runs if account["cost_7d_usd"] is None else f"{runs} ({money(account['cost_7d_usd'])})"


def account_lines(handle: str, a: dict) -> list[str]:
    """What ``show`` prints of an account."""
    held = ", ".join(f"run #{run['run_id']} ({run['project']}, {run['state']})" for run in a["held_by"]) or "no run"
    reported = f", reported {when(a['last_usage_at'])} UTC" if a["last_usage_at"] else ""
    lines = [
        f"Account {a['name']} ({a['env_var']}, {a['provider']}) of {at(handle)}: {a['state']}.",
        f"  Workers: {', '.join(a['workers'])}; expires: {when(a['expires_at']) if a['expires_at'] else 'never'}.",
        f"  Weight {a['weight']}, priority {a['priority']}; last taken {when(a['last_leased_at'])} UTC.",
        f"  Last usage: {usage_text(a['last_usage'])}{reported}.",
    ]
    if a["cost_24h_usd"] is None:  # a service account: the cost of a run is its model account's
        lines.append(f"  Runs: {a['runs_24h']} in 24 hours, {a['runs_7d']} in 7 days.")
    else:
        lines.append(
            f"  Runs: {a['runs_24h']} in 24 hours ({money(a['cost_24h_usd'])}), {a['runs_7d']} in 7 days"
            f" ({money(a['cost_7d_usd'])}, estimated from their usage)."
        )
    lines.append(f"  Held by: {held}.")
    return lines


def written(ctx: HubContext, method: str, body: dict | None, verb: str) -> Result:
    """Send ``body`` to the account and say what it is now."""
    account = AgentAccounts.of(ctx).account(method, ctx.args.handle, ctx.args.name, body)
    return Result(account, Text(account_line(ctx.args.handle, account, verb)))


ONE = (
    Arg("handle", metavar="HANDLE", help="the agent's handle, with or without @"),
    Arg("name", metavar="NAME", help="the account's name, unique among the agent's accounts"),
    PROJECT,
)
ACCOUNT = returns_object(*ACCOUNT_KEYS, schema="AgentAccount")
WEIGHTS = (
    Arg("--weight", type=int, metavar="N", help=f"0 to {MAX_WEIGHT}: its share in a weighted pool"),
    Arg("--priority", type=int, metavar="N", help=f"0 to {MAX_PRIORITY}: the lower, the sooner"),
)
EXPIRES = Arg("--expires", metavar="YYYY-MM-DD", help="its new end, 00:00 UTC")


def after_json(parser: argparse.ArgumentParser, *args: Arg) -> None:
    """Options that come after --json in the help, as the command has always listed them."""
    for arg in args:
        arg.add_to(parser)


# Reading


class AccountList(HubCommand):
    name = "list"
    help = "the accounts of the agents of your projects, or of one agent: state, usage, runs, never a value"
    arguments = (
        Arg("handle", metavar="HANDLE", nargs="?", help="only this agent's, with or without @"),
        Arg("--project", help="only this project's agents; with HANDLE, " + PROJECT_HELP),
    )
    output = returns_array(*LISTED_KEYS, schema="ListedAgentAccount")
    columns: ClassVar[tuple[str, ...]] = (
        "PROJECT",
        "AGENT",
        "NAME",
        "VARIABLE",
        "PROVIDER",
        "STATE",
        "WORKERS",
        "WEIGHT/PRIO",
        "USAGE",
        "RUNS 24H/7D",
        "HELD BY",
        "LAST USED",
    )
    empty = "No agent of a project you hold a grant on has an account; `hub agent account add` gives one."

    def run(self, ctx: HubContext) -> Result:
        query = {}
        if ctx.args.handle is not None:
            query = {"project": ctx.project, "handle": ctx.args.handle.removeprefix("@")}
        elif ctx.args.project:
            query = {"project": ctx.args.project}
        found = ListedAccounts(ctx.hub).list(query)
        return Result(found, Table(self.columns, [self.row(a) for a in found], self.empty))

    @staticmethod
    def row(a: dict) -> tuple:
        return (
            a["project"],
            f"@{a['handle']}",
            a["name"],
            a["env_var"],
            a["provider"],
            a["state"],
            ", ".join(a["workers"]),
            f"{a['weight']}/{a['priority']}",
            usage_text(a["last_usage"]),
            runs_text(a),
            " ".join(f"#{run['run_id']}" for run in a["held_by"]) or "-",
            when(a["last_leased_at"]),
        )


class AccountShow(HubCommand):
    name = "show"
    help = "one account of an agent: state, last usage, runs and cost, runs holding it"
    arguments = ONE
    output = ACCOUNT

    def check(self, args: argparse.Namespace) -> None:
        checked(args)

    def run(self, ctx: HubContext) -> Result:
        found = AgentAccounts.of(ctx).account("GET", ctx.args.handle, ctx.args.name)
        return Result(found, Text(*account_lines(ctx.args.handle, found)))


# Writing


class AccountAdd(HubCommand):
    name = "add"
    help = "give an agent an account, or replace the one of that name (the value comes from stdin or a prompt)"
    arguments = ONE
    output = returns_object(*ACCOUNT_KEYS, "created", schema="AgentAccountWritten")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        super().configure(parser)
        after_json(
            parser,
            Arg("--env-var", required=True, metavar="VAR", help="the variable it sets in the agent's runs"),
            Arg("--worker", action="append", required=True, metavar="NAME", help="a worker it is bound to; repeat it"),
            Arg("--expires", metavar="YYYY-MM-DD", help="the day it ends, 00:00 UTC"),
            *WEIGHTS,
        )

    def check(self, args: argparse.Namespace) -> None:
        why = refusal(args) or env_name_refusal(args.env_var)
        if why is not None:
            raise UsageError(why)
        if args.expires is not None:
            day(args.expires)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        hub = ctx.hub  # signed in before the value is asked for: not signed in costs no paste
        body = {"env_var": args.env_var, "workers": args.worker, "value": read(args)} | weights(args)
        if args.expires is not None:
            body["expires_at"] = day(args.expires)
        account = AgentAccounts(hub, ctx.project).account("PUT", args.handle, args.name, body)
        verb = "Created" if account["created"] else "Replaced"
        return Result(account, Text(account_line(args.handle, account, verb)))


class AccountRenew(HubCommand):
    name = "renew"
    help = "write a new value for an account, which stays the same account"
    arguments = ONE
    output = ACCOUNT

    def configure(self, parser: argparse.ArgumentParser) -> None:
        super().configure(parser)
        after_json(parser, EXPIRES)

    def check(self, args: argparse.Namespace) -> None:
        checked(args)

    def run(self, ctx: HubContext) -> Result:
        hub = ctx.hub  # signed in before the value is asked for
        body = {"value": read(ctx.args)}
        if ctx.args.expires is not None:
            body["expires_at"] = day(ctx.args.expires)  # checked after the value, as it always was
        renewed = AgentAccounts(hub, ctx.project).account("PATCH", ctx.args.handle, ctx.args.name, body)
        return Result(renewed, Text(account_line(ctx.args.handle, renewed, "Renewed")))


class AccountSet(HubCommand):
    name = "set"
    help = "change the weight, priority, workers or end of an account"
    arguments = ONE
    output = ACCOUNT

    def configure(self, parser: argparse.ArgumentParser) -> None:
        super().configure(parser)
        after_json(
            parser,
            *WEIGHTS,
            Arg("--worker", action="append", metavar="NAME", help="the workers it is bound to; repeat it"),
        )
        ends = parser.add_mutually_exclusive_group()
        ends.add_argument(*EXPIRES.flags, **EXPIRES.options)
        ends.add_argument("--no-expiry", action="store_true", help="drop its end")

    def check(self, args: argparse.Namespace) -> None:
        checked(args)
        change_body(args)

    def run(self, ctx: HubContext) -> Result:
        return written(ctx, "PATCH", change_body(ctx.args), "Changed")


class AccountPause(HubCommand):
    name = "pause"
    help = "keep new runs off an account; a run that holds it keeps it"
    arguments = ONE
    output = ACCOUNT

    def check(self, args: argparse.Namespace) -> None:
        checked(args)

    def run(self, ctx: HubContext) -> Result:
        return written(ctx, "PATCH", {"paused": True}, "Paused")


class AccountResume(HubCommand):
    name = "resume"
    help = "let new runs take a paused account again"
    arguments = ONE
    output = ACCOUNT

    def check(self, args: argparse.Namespace) -> None:
        checked(args)

    def run(self, ctx: HubContext) -> Result:
        return written(ctx, "PATCH", {"paused": False}, "Resumed")


class AccountRemove(HubCommand):
    name = "remove"
    help = "delete an account: its value and bindings go, its leases out are revoked"
    arguments = ONE

    def configure(self, parser: argparse.ArgumentParser) -> None:
        super().configure(parser)
        parser.set_defaults(json=False)

    def check(self, args: argparse.Namespace) -> None:
        checked(args)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        AgentAccounts.of(ctx).account("DELETE", args.handle, args.name)
        said = (
            f"Deleted account {args.name} of {at(args.handle)}: its value and bindings are gone from the "
            "hub, and its leases still out are revoked. Revoke it where it was made as well."
        )
        return Result(None, Text(said))


AGENT_ACCOUNT = Group(  # hub agent account
    "account",
    "the accounts of an agent: env secrets its runs take on the workers bound",
    [
        AccountList(),
        AccountShow(),
        AccountAdd(),
        AccountRenew(),
        AccountSet(),
        AccountPause(),
        AccountResume(),
        AccountRemove(),
    ],
)
