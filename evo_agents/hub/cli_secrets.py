"""``evo-agents hub secret set|list|delete`` and ``hub account``: the owner's secrets on the hub, and their env secrets
as accounts (``docs/credentials.md``).

``set`` writes a secret whole, value included (PUT /v1/secrets/{name}), creating it or replacing the one of that name.
The value never comes from the command line, where ``ps`` and the shell's history would see it: it is read from stdin
when stdin is not a terminal, one final line break dropped, else asked for with ``getpass``, which does not echo it.
The value is checked here for what the hub refuses anyway (empty, too long, a NUL, more than one line for kind git),
and never printed, not even in an error. Every check of the arguments comes before the value is read, so a mistyped
flag never costs a paste. ``--expires YYYY-MM-DD`` ends the secret at 00:00 UTC of that day, as GitLab ends an access
token on its expiry date.

``list`` prints the caller's own secrets without their values (GET /v1/secrets), and ``delete`` deletes one (DELETE
/v1/secrets/{name}): its value and bindings go, and the leases of it still out are revoked. ``hub run credentials``,
in ``run_cli``, lists the leases one run got of them.

``hub account`` sees the caller's env secrets as accounts, one of which a run takes among several for the same
variable. ``account list`` prints them, never a value (GET /v1/accounts); ``account set`` changes the weight or
priority of one, ``account pause`` and ``account resume`` keep new runs off it or let them take it again (PATCH
/v1/accounts/{name}). ``account policy PROJECT`` prints the caller's policy for a project (GET
/v1/account-policies/{project}); with a flag it sets it, the flags given over the policy as it is now (PUT), and
``--reset`` drops it, so the defaults hold again (DELETE). Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from datetime import UTC, datetime
from urllib.parse import quote

from evo_agents.hub.accounts import (
    MAX_COOLDOWN_SECONDS,
    MAX_FAILOVERS,
    MAX_PRIORITY,
    MAX_WEIGHT,
    MIN_COOLDOWN_SECONDS,
    STRATEGIES,
)
from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in, _table, _when
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.credentials import MAX_SECRET_BYTES, SECRET_KINDS, SECRET_NAME, env_name_refusal
from evo_agents.hub.curator import money

EXIT_USAGE = 2
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The keys of a secret in `hub secret list --json` (the contract, `evo-agents hub contract print`).
SECRET_KEYS = (
    "name",
    "kind",
    "env_var",
    "url_prefix",
    "username",
    "projects",
    "workers",
    "expires_at",
    "created_at",
    "updated_at",
)


class Refused(Exception):
    """The arguments or the value cannot make a secret; ``str`` says why, never with the value."""


def _secret_path(name: str) -> str:
    return f"/v1/secrets/{quote(name, safe='')}"


def _usage_error(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def check_name(name: str) -> None:
    if not SECRET_NAME.fullmatch(name):
        raise Refused(
            f"{name!r} is not a secret's name: 1 to 64 of a-z, 0-9, '.', '_' and '-', starting with a letter or digit"
        )


def expires_at(day: str) -> str:
    """``YYYY-MM-DD`` as the moment the secret ends: 00:00 UTC of that day, in ISO 8601."""
    try:
        moment = datetime.strptime(day, "%Y-%m-%d") if DAY.fullmatch(day) else None
    except ValueError:
        moment = None
    if moment is None:
        raise Refused(f"--expires takes a day as YYYY-MM-DD, such as 2027-01-31, not {day!r}")
    return moment.replace(tzinfo=UTC).isoformat()


def target(args) -> dict:
    """The fields of the secret's kind from ``args``; Refused for a flag of the other kind or one missing."""
    if args.kind == "env":
        if args.url_prefix is not None or args.username is not None:
            raise Refused("--url-prefix and --username belong to --kind git, not env")
        if args.env_var is None:
            raise Refused("--kind env needs --env-var, the variable of the agent's environment it sets")
        refusal = env_name_refusal(args.env_var)
        if refusal is not None:
            raise Refused(f"--env-var {refusal}")
        return {"env_var": args.env_var}
    if args.env_var is not None:
        raise Refused("--env-var belongs to --kind env, not git")
    if args.url_prefix is None:
        raise Refused("--kind git needs --url-prefix, the https prefix of the origins it answers for")
    return {"url_prefix": args.url_prefix, **({"username": args.username} if args.username is not None else {})}


def read_value(name: str, stdin=None) -> str:
    """The secret's value: stdin when it is not a terminal, one final line break dropped; else asked for without
    echo. Refused when nothing came."""
    stream = sys.stdin if stdin is None else stdin
    if stream is not None and stream.isatty():
        try:
            value = getpass.getpass(f"Value of secret {name} (not shown): ")
        except EOFError:
            value = ""
    else:
        value = stream.read() if stream is not None else ""
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    if not value:
        raise Refused(
            "no value: pipe it on stdin, such as `pbpaste | evo-agents hub secret set NAME ...`, or run the command "
            "in a terminal to be asked for it"
        )
    return value


def check_value(kind: str, value: str) -> None:
    """What the hub would refuse in the value, said without it."""
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise Refused(f"the value is {len(value.encode())} bytes; a secret's value is at most {MAX_SECRET_BYTES}")
    if "\x00" in value:
        raise Refused("the value holds a NUL character, which a secret's value may not")
    if kind == "git" and ("\n" in value or "\r" in value):
        raise Refused("the value of a secret of kind git is one line: git's credential protocol has no other")


def _target_text(secret: dict) -> str:
    if secret["kind"] == "env":
        return secret["env_var"] or "-"
    return f"{secret['url_prefix']} as {secret['username']}"


def _workers_text(secret: dict) -> str:
    return ", ".join(secret["workers"]) if secret["workers"] else "any worker of yours"


@_client_command
def cmd_set(args) -> int:
    try:
        check_name(args.name)
        body = {"kind": args.kind, **target(args), "projects": args.project}
        if args.worker:
            body["workers"] = args.worker
        if args.expires is not None:
            body["expires_at"] = expires_at(args.expires)
    except Refused as exc:
        return _usage_error(str(exc))
    hub, _ = _signed_in()  # before the value is asked for: not signed in costs no paste
    try:
        value = read_value(args.name)
        check_value(args.kind, value)
    except Refused as exc:
        return _usage_error(str(exc))
    written = hub.call("PUT", _secret_path(args.name), {**body, "value": value})
    verb = "Created" if written["created"] else "Replaced"
    projects = ", ".join(written["projects"])
    ends = f"; it ends {_when(written['expires_at'])} UTC" if written["expires_at"] else ""
    print(
        f"{verb} secret {written['name']} ({written['kind']}, {_target_text(written)}) for project(s) {projects}, "
        f"on {_workers_text(written)}{ends}. The hub keeps the value sealed and never shows it again."
    )
    return 0


@_client_command
def cmd_list(args) -> int:
    hub, _ = _signed_in()
    found = hub.call("GET", "/v1/secrets")
    if args.json:
        _print_json(found)
        return 0
    if not found:
        print("You keep no secret on this hub; `evo-agents hub secret set` adds one.")
        return 0
    rows = [
        (
            s["name"],
            s["kind"],
            _target_text(s),
            ", ".join(s["projects"]),
            ", ".join(s["workers"]) or "any",
            _when(s["expires_at"]),
            _when(s["updated_at"]),
        )
        for s in found
    ]
    _table(("NAME", "KIND", "TARGET", "PROJECTS", "WORKERS", "EXPIRES (UTC)", "UPDATED (UTC)"), rows)
    return 0


@_client_command
def cmd_delete(args) -> int:
    try:
        check_name(args.name)
    except Refused as exc:
        return _usage_error(str(exc))
    hub, _ = _signed_in()
    hub.call("DELETE", _secret_path(args.name))
    print(
        f"Deleted secret {args.name}: its value and bindings are gone from the hub, and its leases still out are "
        "revoked. Revoke it where it was made as well."
    )
    return 0


def register_secrets(hsub) -> None:
    secret = hsub.add_parser(
        "secret", help="your secrets on the hub, which runs of your projects get as leases; values are never shown"
    )
    ssub = secret.add_subparsers(dest="secret_command", required=True)

    put = ssub.add_parser(
        "set",
        help="create a secret, or replace it whole; the value comes from stdin, or is asked for without echo",
    )
    put.add_argument("name", metavar="NAME", help="1 to 64 of a-z, 0-9, '.', '_' and '-'")
    put.add_argument(
        "--kind",
        required=True,
        choices=SECRET_KINDS,
        help="env: a variable of the agent's environment; git: what git's credential helper answers",
    )
    put.add_argument("--env-var", metavar="VAR", help="kind env: the variable it sets, such as CLAUDE_CODE_OAUTH_TOKEN")
    put.add_argument(
        "--url-prefix",
        metavar="URL",
        help="kind git: the https prefix of the origins it answers for, such as https://gitlab.example.org/group",
    )
    put.add_argument("--username", metavar="USER", help="kind git: the user git sends with the value (default oauth2)")
    put.add_argument(
        "--project",
        required=True,
        action="append",
        metavar="PROJECT",
        help="a project whose runs get it, on which you hold writer; repeat it for several",
    )
    put.add_argument(
        "--worker",
        action="append",
        metavar="NAME",
        help="only runs on this worker of yours get it; repeat it for several (default: any worker of yours)",
    )
    put.add_argument(
        "--expires", metavar="YYYY-MM-DD", help="no run gets it from 00:00 UTC of this day on (default: no end)"
    )
    put.set_defaults(func=cmd_set)

    listed = ssub.add_parser("list", help="your secrets: kind, target, projects, workers and dates, never the value")
    json_option(listed, returns_array(*SECRET_KEYS, schema="Secret"))
    listed.set_defaults(func=cmd_list)

    delete = ssub.add_parser(
        "delete", help="delete a secret: its value and bindings go, and its leases still out are revoked"
    )
    delete.add_argument("name", metavar="NAME")
    delete.set_defaults(func=cmd_delete)

    register_accounts(hsub)


# `hub account`: the env secrets as accounts (docs/credentials.md, section Accounts)


# The keys of an account in `hub account list --json` and `set|pause|resume --json` (`evo-agents hub contract print`).
ACCOUNT_KEYS = (
    "name",
    "env_var",
    "projects",
    "workers",
    "weight",
    "priority",
    "paused_at",
    "cooldown_until",
    "cooldown_reason",
    "last_leased_at",
    "expires_at",
    "state",
    "last_usage",
    "last_usage_at",
    "runs_24h",
    "runs_7d",
    "cost_24h_usd",
    "cost_7d_usd",
    "held_by",
)
# The keys of a policy in `hub account policy --json`.
POLICY_KEYS = ("strategy", "cooldown_seconds", "failover", "max_failovers", "project", "updated_at")
POLICY_FLAGS = ("strategy", "cooldown_seconds", "failover", "max_failovers")


def _account_path(name: str) -> str:
    return f"/v1/accounts/{quote(name, safe='')}"


def _policy_path(project: str) -> str:
    return f"/v1/account-policies/{quote(project, safe='')}"


BOUNDS = {
    "weight": (0, MAX_WEIGHT, "--weight"),
    "priority": (0, MAX_PRIORITY, "--priority"),
    "cooldown_seconds": (MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS, "--cooldown"),
    "max_failovers": (0, MAX_FAILOVERS, "--max-failovers"),
}


def _out_of_bounds(given: dict) -> str | None:
    """Why a number of ``given`` is not one the hub takes; None when each is."""
    for key, value in given.items():
        if key in BOUNDS and not BOUNDS[key][0] <= value <= BOUNDS[key][1]:
            low, high, flag = BOUNDS[key]
            return f"{flag} is a whole number from {low} to {high}, not {value}"
    return None


def _account_line(account: dict) -> str:
    return (
        f"Account {account['name']} ({account['env_var']}): {account['state']}, weight {account['weight']}, "
        f"priority {account['priority']}."
    )


def _change_account(args, body: dict) -> int:
    if not SECRET_NAME.fullmatch(args.name):
        return _usage_error(f"{args.name!r} is not a secret's name")
    hub, _ = _signed_in()
    account = hub.call("PATCH", _account_path(args.name), body)
    if args.json:
        _print_json(account)
    else:
        print(_account_line(account))
    return 0


@_client_command
def cmd_account_list(args) -> int:
    hub, _ = _signed_in()
    found = hub.call("GET", "/v1/accounts")
    if args.json:
        _print_json(found)
        return 0
    if not found:
        print("You keep no env secret on this hub; `evo-agents hub secret set --kind env` adds one.")
        return 0
    rows = [
        (
            a["name"],
            a["env_var"],
            a["state"],
            a["weight"],
            a["priority"],
            _when(a["cooldown_until"]),
            _usage_text(a["last_usage"]),
            f"{a['runs_24h']}/{a['runs_7d']}",
            money(a["cost_7d_usd"]),
            " ".join(f"#{run['run_id']}" for run in a["held_by"]) or "-",
            ", ".join(a["projects"]),
            ", ".join(a["workers"]) or "any",
        )
        for a in found
    ]
    header = ("NAME", "VARIABLE", "STATE", "WEIGHT", "PRIORITY", "COOLS UNTIL", "USAGE", "RUNS 24H/7D", "COST 7D")
    _table((*header, "HELD BY", "PROJECTS", "WORKERS"), rows)
    return 0


def _percent(share) -> str:
    """A utilization the CLI gave, as a fraction (0.85) or a percent (85), as `` 85%``; empty for none."""
    if not isinstance(share, int | float):
        return ""
    return f" {share * 100 if share <= 1 else share:.0f}%"


def _usage_text(usage: dict | None) -> str:
    """The last usage a run's CLI reported, short: ``five_hour 85% (allowed_warning)``; ``-`` for none."""
    if not usage:
        return "-"
    return f"{usage.get('rate_limit_type') or 'usage'}{_percent(usage.get('utilization'))} ({usage['status']})"


def _state_text(account: dict) -> str:
    state = account["state"]
    if state == "cooling":
        return f"cooling until {_when(account['cooldown_until'])} UTC ({account['cooldown_reason'] or 'usage limit'})"
    if state == "paused":
        return f"paused since {_when(account['paused_at'])} UTC"
    if state == "expired":
        return f"expired at {_when(account['expires_at'])} UTC"
    return "ready"


@_client_command
def cmd_account_show(args) -> int:
    if not SECRET_NAME.fullmatch(args.name):
        return _usage_error(f"{args.name!r} is not a secret's name")
    hub, _ = _signed_in()
    a = hub.call("GET", _account_path(args.name))
    if args.json:
        _print_json(a)
        return 0
    held = ", ".join(f"run #{run['run_id']} ({run['project']}, {run['state']})" for run in a["held_by"]) or "no run"
    usage = _usage_text(a["last_usage"])
    reported = f", reported {_when(a['last_usage_at'])} UTC" if a["last_usage_at"] else ""
    print(f"Account {a['name']} ({a['env_var']}): {_state_text(a)}.")
    print(f"  Projects: {', '.join(a['projects']) or '-'}; workers: {', '.join(a['workers']) or 'any of yours'}.")
    print(f"  Weight {a['weight']}, priority {a['priority']}; last taken {_when(a['last_leased_at'])} UTC.")
    print(f"  Last usage: {usage}{reported}.")
    print(f"  Runs: {a['runs_24h']} in 24 hours ({money(a['cost_24h_usd'])}), {a['runs_7d']} in 7 days", end="")
    print(f" ({money(a['cost_7d_usd'])}, estimated from their usage).")
    print(f"  Held by: {held}.")
    return 0


@_client_command
def cmd_account_set(args) -> int:
    body = {key: getattr(args, key) for key in ("weight", "priority") if getattr(args, key) is not None}
    if not body:
        return _usage_error("give --weight, --priority or both")
    if (refusal := _out_of_bounds(body)) is not None:
        return _usage_error(refusal)
    return _change_account(args, body)


@_client_command
def cmd_account_pause(args) -> int:
    return _change_account(args, {"paused": True})


@_client_command
def cmd_account_resume(args) -> int:
    return _change_account(args, {"paused": False})


def _print_policy(policy: dict) -> None:
    failover = f"on, at most {policy['max_failovers']} time(s)" if policy["failover"] else "off"
    origin = f"set {_when(policy['updated_at'])} UTC" if policy["updated_at"] else "the defaults"
    print(
        f"Project {policy['project']} ({origin}): strategy {policy['strategy']}, cooldown "
        f"{policy['cooldown_seconds']} s, failover {failover}."
    )


@_client_command
def cmd_account_policy(args) -> int:
    given = {key: getattr(args, key) for key in POLICY_FLAGS if getattr(args, key) is not None}
    if args.reset and given:
        return _usage_error("--reset drops the policy: give it alone")
    if (refusal := _out_of_bounds(given)) is not None:
        return _usage_error(refusal)
    hub, _ = _signed_in()
    path = _policy_path(args.project)
    if args.reset:
        hub.call("DELETE", path)
        policy = hub.call("GET", path)
    elif given:
        now = hub.call("GET", path)
        policy = hub.call("PUT", path, {**{key: now[key] for key in POLICY_FLAGS}, **given})
    else:
        policy = hub.call("GET", path)
    if args.json:
        _print_json(policy)
    else:
        _print_policy(policy)
    return 0


def _named(sub, command: str, help: str, func) -> argparse.ArgumentParser:
    parser = sub.add_parser(command, help=help)
    parser.add_argument("name", metavar="NAME", help="one of your env secrets")
    json_option(parser, returns_object(*ACCOUNT_KEYS, schema="Account"))
    parser.set_defaults(func=func)
    return parser


def register_accounts(hsub) -> None:
    account = hsub.add_parser(
        "account", help="your env secrets as accounts runs rotate among, and your policy for each project"
    )
    asub = account.add_subparsers(dest="account_command", required=True)

    listed = asub.add_parser(
        "list", help="your accounts: variable, state, weight, priority, usage, runs and cost, never the value"
    )
    json_option(listed, returns_array(*ACCOUNT_KEYS, schema="Account"))
    listed.set_defaults(func=cmd_account_list)

    _named(
        asub,
        "show",
        "one account: state, last usage, runs and cost over 24 hours and 7 days, runs holding it",
        cmd_account_show,
    )
    changed = _named(asub, "set", "change the weight or priority of an account", cmd_account_set)
    changed.add_argument(
        "--weight",
        type=int,
        metavar="N",
        help=f"0 to {MAX_WEIGHT}: its share in a weighted-round-robin pool; 0 leaves the pool",
    )
    changed.add_argument(
        "--priority",
        type=int,
        metavar="N",
        help=f"0 to {MAX_PRIORITY}: the lower, the sooner a fill-first pool takes it",
    )
    _named(asub, "pause", "keep new runs off an account; a run that holds it keeps it", cmd_account_pause)
    _named(asub, "resume", "let new runs take a paused account again", cmd_account_resume)

    policy = asub.add_parser(
        "policy", help="show your policy for a project; with a flag, set it over the policy as it is now"
    )
    policy.add_argument("project", metavar="PROJECT")
    policy.add_argument("--strategy", choices=STRATEGIES, help="how a run picks among your accounts")
    policy.add_argument(
        "--cooldown",
        dest="cooldown_seconds",
        type=int,
        metavar="SECONDS",
        help="how long an account cools down after a usage limit that gave no reset time",
    )
    policy.add_argument(
        "--failover",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="whether a run that hits a usage limit goes on with another account",
    )
    policy.add_argument(
        "--max-failovers",
        type=int,
        metavar="N",
        help=f"0 to {MAX_FAILOVERS}: at most how many times a run moves on",
    )
    policy.add_argument("--reset", action="store_true", help="drop your policy: the defaults hold again")
    json_option(policy, returns_object(*POLICY_KEYS, schema="AccountPolicy"))
    policy.set_defaults(func=cmd_account_policy)
