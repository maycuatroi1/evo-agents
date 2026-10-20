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
``--reset`` drops it, so the defaults hold again (DELETE). ``account list`` and ``account show`` remind the member to
renew an account that ends within RENEW_WITHIN_DAYS days, or has ended.

``account login NAME`` adds a Claude account, or renews the token of one: it runs ``claude setup-token`` unchanged on
the member's machine (``evo_agents.hub.setup_token``), where the member approves in the browser, takes the token it
prints off its screen, and writes it with PUT /v1/secrets/{name} as an env secret for CLAUDE_CODE_OAUTH_TOKEN, ending a
year from today less a day unless ``--expires`` says otherwise. With ``--paste``, when stdin is not a terminal, or
when the screen held no token, the token is read as ``secret set`` reads a value. An API key of the Anthropic Console
is refused, pointing at ANTHROPIC_API_KEY. Renewing an account sends its projects and workers again unless a flag
gives others; PUT keeps the id of the secret, so its weight, priority, pause, cooldown and runs stay. Every check that
needs no token (the name, the day, the projects' writer role, the workers) comes before ``claude setup-token`` runs,
so a token it makes is not lost to a refusal. Standard library only, like the rest of the client.

``account oauth-login NAME`` does the same for an account of Claude's OAuth sign-in (``evo_agents.hub.claude_oauth``):
the member approves on claude.ai, the command trades the code for an access token, a refresh token and the end of the
access token, in memory alone, and writes them with PUT /v1/secrets/{name}: the access token as the value, the rest as
``oauth``, which the hub seals with it. ``account list`` and ``show`` tell such an account by ``auth``: oauth, where an
account of a pasted or ``claude setup-token`` token is token. Running it again for a NAME that exists replaces the
tokens alone, as ``login`` does, with the same secret id.
"""

from __future__ import annotations

import argparse
import getpass
import re
import shutil
import sys
import webbrowser
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from evo_agents.hub import claude_oauth, setup_token
from evo_agents.hub.access import has_role
from evo_agents.hub.accounts import (
    MAX_COOLDOWN_SECONDS,
    MAX_FAILOVERS,
    MAX_PRIORITY,
    MAX_WEIGHT,
    MIN_COOLDOWN_SECONDS,
    RENEW_WITHIN_DAYS,
    STRATEGIES,
)
from evo_agents.hub.cli_client import EXIT_FAILED, _client_command, _print_json, _signed_in, _table, _when
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


NO_VALUE = (
    "no value: pipe it on stdin, such as `pbpaste | evo-agents hub secret set NAME ...`, or run the command in a "
    "terminal to be asked for it"
)


def read_value(name: str, stdin=None, prompt: str | None = None, missing: str = NO_VALUE) -> str:
    """The secret's value: stdin when it is not a terminal, one final line break dropped; else asked for without
    echo, with ``prompt``. Refused with ``missing`` when nothing came."""
    stream = sys.stdin if stdin is None else stdin
    if stream is not None and stream.isatty():
        try:
            value = getpass.getpass(prompt or f"Value of secret {name} (not shown): ")
        except EOFError:
            value = ""
    else:
        value = stream.read() if stream is not None else ""
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    if not value:
        raise Refused(missing)
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
    "auth",
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
            a["auth"],
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
    header = (
        "NAME",
        "VARIABLE",
        "AUTH",
        "STATE",
        "WEIGHT",
        "PRIORITY",
        "COOLS UNTIL",
        "USAGE",
        "RUNS 24H/7D",
        "COST 7D",
    )
    _table((*header, "HELD BY", "PROJECTS", "WORKERS"), rows)
    now = datetime.now(UTC)
    reminders = []
    for a in found:
        text, due = _expiry(a, now)
        if due:
            reminders.append(f"Account {a['name']}: expires {text}.")
    if reminders:
        print("\n" + "\n".join(reminders))
    return 0


def _renew_command(account: dict) -> str:
    if account["env_var"] == setup_token.ENV_VAR:
        command = "oauth-login" if account.get("auth") == "oauth" else "login"
        return f"`evo-agents hub account {command} {account['name']}`"
    return f"`evo-agents hub secret set {account['name']} --kind env --env-var {account['env_var']} ...`"


def _expiry(account: dict, now: datetime) -> tuple[str, bool]:
    """When the account ends, as ``account show`` says it, and whether it is due for renewal: it ends within
    RENEW_WITHIN_DAYS days, or has ended."""
    if not account["expires_at"]:
        return "never", False
    when = f"{_when(account['expires_at'])} UTC"
    if account.get("auth") == "oauth":  # the end of its access token, which its refresh token replaces
        return f"access token until {when}", False
    left = datetime.fromisoformat(account["expires_at"]) - now
    if left > timedelta(days=RENEW_WITHIN_DAYS):
        return when, False
    renew = f"renew it with {_renew_command(account)}"
    if left <= timedelta(0):
        return f"{when}, already past, so no run takes it: {renew}", True
    days = f"in {left.days} day(s)" if left.days else "in less than a day"
    return f"{when}, {days}: {renew}", True


def _percent(share) -> str:
    """A utilization the CLI gave, a fraction of the limit (0.85), as `` 85%``; above 1 is the limit, `` 100%``, as
    the web reads it (docs/credentials.md, section "Accounts"); empty for none."""
    if isinstance(share, bool) or not isinstance(share, int | float):
        return ""
    return f" {min(max(share, 0), 1) * 100:.0f}%"


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
    else:
        _print_account(a)
    return 0


def _print_account(a: dict) -> None:
    """An account as ``account show`` prints it."""
    held = ", ".join(f"run #{run['run_id']} ({run['project']}, {run['state']})" for run in a["held_by"]) or "no run"
    usage = _usage_text(a["last_usage"])
    reported = f", reported {_when(a['last_usage_at'])} UTC" if a["last_usage_at"] else ""
    print(f"Account {a['name']} ({a['env_var']}): {_state_text(a)}.")
    if a.get("auth") == "oauth":
        print("  Signed in with OAuth: the hub keeps its tokens sealed; renew with `account oauth-login`.")
    print(f"  Projects: {', '.join(a['projects']) or '-'}; workers: {', '.join(a['workers']) or 'any of yours'}.")
    print(f"  Expires: {_expiry(a, datetime.now(UTC))[0]}.")
    print(f"  Weight {a['weight']}, priority {a['priority']}; last taken {_when(a['last_leased_at'])} UTC.")
    print(f"  Last usage: {usage}{reported}.")
    print(f"  Runs: {a['runs_24h']} in 24 hours ({money(a['cost_24h_usd'])}), {a['runs_7d']} in 7 days", end="")
    print(f" ({money(a['cost_7d_usd'])}, estimated from their usage).")
    print(f"  Held by: {held}.")


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


# `hub account login`: a Claude account from `claude setup-token`

NO_CLAUDE = (
    "there is no `claude` on PATH to run `claude setup-token` with. Install Claude Code "
    "(https://code.claude.com/docs/en/setup), or run `claude setup-token` where it is installed and give the token "
    "it prints to `evo-agents hub account login {name} --paste`, which asks for it without echo, or on stdin, such "
    "as `pbpaste | evo-agents hub account login {name}`"
)
NO_TOKEN = (
    "no token: run the command in a terminal, where it runs `claude setup-token` for you or asks for the token, or "
    "pipe the token on stdin, such as `pbpaste | evo-agents hub account login NAME`"
)
API_KEY = (
    "that is an API key of the Anthropic Console (sk-ant-api...), not a token of `claude setup-token`: an API key "
    "goes in ANTHROPIC_API_KEY, with `evo-agents hub secret set NAME --kind env --env-var ANTHROPIC_API_KEY "
    "--project PROJECT`"
)


def _say(text: str) -> None:
    """A line for the member at the terminal, on stderr, so stdout holds the account alone."""
    print(text, file=sys.stderr)


def login_ends(day: str | None, now: datetime) -> str:
    """When the account ends, in ISO 8601: 00:00 UTC of ``day`` (YYYY-MM-DD), which is to come, or of a year from
    today (UTC) less a day when there is none. Refused for a day that is not one, or is past."""
    if day is None:
        return expires_at(setup_token.default_end(now.date()).isoformat())
    ends = expires_at(day)
    if datetime.fromisoformat(ends) <= now:
        raise Refused(f"--expires {day} is past already: an account ends on a day to come")
    return ends


def login_body(args, existing: dict | None, ends: str) -> dict:
    """What PUT /v1/secrets/{name} gets for the account, but its token: the projects and workers given, else those of
    the account when it is one already. Refused for a secret of that name that is not an account for
    CLAUDE_CODE_OAUTH_TOKEN, and for a new account without a project."""
    var = setup_token.ENV_VAR
    if existing is not None and (existing["kind"], existing["env_var"]) != ("env", var):
        what = f"sets {existing['env_var']}" if existing["kind"] == "env" else f"is of kind {existing['kind']}"
        raise Refused(f"your secret {args.name} {what}, not {var}: give the Claude account another name")
    projects = args.project or (existing["projects"] if existing else [])
    if not projects:
        raise Refused("a new account needs --project, a project whose runs take it; repeat it for several")
    workers = args.worker if args.worker is not None else (existing["workers"] if existing else [])
    return {"kind": "env", "env_var": var, "projects": projects, "workers": workers, "expires_at": ends}


def check_bindings(hub, body: dict) -> None:
    """Refused for a project of ``body`` the caller does not hold the writer role on, or a worker that is not one of
    theirs, as PUT /v1/secrets would refuse them: asked before ``claude setup-token`` makes a token, so none is made
    to be lost."""
    me = hub.call("GET", "/v1/auth/whoami")
    roles = {grant["project"]: grant["role"] for grant in me["grants"]}
    for project in body["projects"]:
        if not has_role(roles.get(project), "writer"):
            held = roles.get(project) or "no role"
            raise Refused(f"an account for project {project} needs the writer role on it; you hold {held}")
    if body["workers"]:
        mine = {w["name"].lower() for w in hub.call("GET", "/v1/workers") if w["owner"] == me["login"]}
        for worker in body["workers"]:
            if worker.lower() not in mine:
                raise Refused(f"{worker} is not a worker of yours that is not revoked: see GET /v1/workers")


def login_token(value: str) -> str:
    """The token as given, without the spaces and line breaks around it; Refused for an API key, or for what cannot
    be one token."""
    token = value.strip()
    if not token:
        raise Refused(NO_TOKEN)
    if setup_token.is_api_key(token):
        raise Refused(API_KEY)
    if any(char.isspace() for char in token):
        raise Refused("the token holds a space or a line break: give it as one line")
    check_value("env", token)
    return token


def _login_intro(name: str, existing: dict | None, body: dict) -> str:
    projects = ", ".join(body["projects"])
    workers = ", ".join(body["workers"]) or "any worker of yours"
    ends = f"{_when(body['expires_at'])} UTC"
    if existing is None:
        return f"Adding account {name} ({body['env_var']}) for project(s) {projects}, on {workers}; it ends {ends}."
    return (
        f"{name} is an account of yours already: this replaces its token, which ends {ends}, for project(s) "
        f"{projects}, on {workers}. Its weight, priority, pause, cooldown and runs stay as they are."
    )


def _login_value(name: str, claude: str | None) -> str:
    """The token: what ``claude setup-token`` printed when ``claude`` runs it and its screen held one, else read
    from stdin or asked for without echo."""
    if claude is not None:
        _say(
            "Running `claude setup-token`: sign in to Claude in the browser it opens. The token it prints goes to the "
            "hub sealed, and is not shown here."
        )
        token = setup_token.run(claude)
        if token is not None:
            _say("Took the token `claude setup-token` printed.")
            return token
        _say("Found no token in what `claude setup-token` printed: paste it, or press Enter to stop.")
    prompt = f"Token of `claude setup-token` for account {name} (not shown): "
    return read_value(name, prompt=prompt, missing=NO_TOKEN)


@_client_command
def cmd_account_login(args) -> int:
    try:
        check_name(args.name)
        ends = login_ends(args.expires, datetime.now(UTC))
    except Refused as exc:
        return _usage_error(str(exc))
    hub, _ = _signed_in()
    claude = None
    if not args.paste and sys.stdin is not None and sys.stdin.isatty():
        claude = shutil.which("claude")
        if claude is None:
            print(f"error: {NO_CLAUDE.format(name=args.name)}", file=sys.stderr)
            return EXIT_FAILED
    existing = next((s for s in hub.call("GET", "/v1/secrets") if s["name"] == args.name), None)
    try:
        body = login_body(args, existing, ends)
        check_bindings(hub, body)
        _say(_login_intro(args.name, existing, body))
        token = login_token(_login_value(args.name, claude))
    except Refused as exc:
        return _usage_error(str(exc))
    written = hub.call("PUT", _secret_path(args.name), {**body, "value": token})
    account = hub.call("GET", _account_path(args.name))
    if args.json:
        _print_json(account)
        return 0
    if written["created"]:
        print(f"Created account {args.name}. The hub keeps its token sealed and never shows it again.")
    else:
        print(
            f"Replaced the token of account {args.name}; its weight, priority, pause, cooldown and runs stay. The hub "
            "keeps the token sealed and never shows it again."
        )
    _print_account(account)
    return 0


OAUTH_PASTE = (
    "no code: pipe the code claude.ai shows after you approve, such as `pbpaste | evo-agents hub account oauth-login "
    "NAME`, or run the command in a terminal"
)


def _oauth_code(name: str, pkce: claude_oauth.Pkce, args) -> str:
    """The authorization code: from the browser's answer on localhost when a terminal and a browser are here, else
    pasted (a code, ``code#state``, or the address the browser went to)."""
    url = claude_oauth.authorize_url(pkce)
    if not args.paste and sys.stdin is not None and sys.stdin.isatty():
        _say(f"Sign in to Claude in the browser, then come back here. If it does not open, go to:\n\n{url}\n")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except webbrowser.Error:
                pass
        try:
            return claude_oauth.wait_for_callback(pkce.state)
        except claude_oauth.ListenError as exc:
            _say(f"{exc}: paste the code claude.ai shows, or the address the browser went to.")
    else:
        _say(f"Open this address, approve, and give the code claude.ai shows:\n\n{url}\n")
    pasted = read_value(name, prompt=f"Code for account {name} (not shown): ", missing=OAUTH_PASTE)
    return claude_oauth.parse_code(pasted, pkce.state)


@_client_command
def cmd_account_oauth_login(args) -> int:
    try:
        check_name(args.name)
    except Refused as exc:
        return _usage_error(str(exc))
    hub, _ = _signed_in()
    existing = next((s for s in hub.call("GET", "/v1/secrets") if s["name"] == args.name), None)
    try:
        body = login_body(args, existing, "")
        body.pop("expires_at")  # the end of the access token, which the tokens carry
        check_bindings(hub, body)
        projects = ", ".join(body["projects"])
        workers = ", ".join(body["workers"]) or "any worker of yours"
        verb = "Adding" if existing is None else "Replacing the tokens of"
        _say(f"{verb} account {args.name} ({body['env_var']}, OAuth) for project(s) {projects}, on {workers}.")
        pkce = claude_oauth.new_pkce()
        code = _oauth_code(args.name, pkce, args)
        tokens = claude_oauth.exchange(code, pkce)
    except (Refused, claude_oauth.OAuthError) as exc:
        return _usage_error(str(exc))
    oauth = {
        "refresh_token": tokens.refresh_token,
        "access_expires_at": tokens.expires_at.isoformat(),
        "account_uuid": tokens.account_uuid,
        "organization_uuid": tokens.organization_uuid,
        "email": tokens.email,
    }
    written = hub.call("PUT", _secret_path(args.name), {**body, "value": tokens.access_token, "oauth": oauth})
    account = hub.call("GET", _account_path(args.name))
    if args.json:
        _print_json(account)
        return 0
    if written["created"]:
        print(f"Created account {args.name} (OAuth). The hub keeps its tokens sealed and never shows them again.")
    else:
        print(
            f"Replaced the tokens of account {args.name}; its weight, priority, pause, cooldown and runs stay. The hub "
            "keeps the tokens sealed and never shows them again."
        )
    _print_account(account)
    return 0


def _register_oauth_login(asub) -> None:
    login = asub.add_parser(
        "oauth-login",
        help="add a Claude account, or renew its tokens, by Claude's OAuth sign-in in your browser: the tokens go to "
        "the hub sealed, never on the screen",
    )
    login.add_argument("name", metavar="NAME", help="1 to 64 of a-z, 0-9, '.', '_' and '-'; an account's to renew it")
    login.add_argument(
        "--project",
        action="append",
        metavar="PROJECT",
        help="a project whose runs take it, on which you hold writer; repeat it for several (a new account needs one; "
        "renewing keeps the account's projects)",
    )
    login.add_argument(
        "--worker",
        action="append",
        metavar="NAME",
        help="only runs on this worker of yours take it; repeat it for several (default: any worker of yours; "
        "renewing keeps the account's workers)",
    )
    login.add_argument("--no-browser", action="store_true", help="print the address to open instead of opening it")
    login.add_argument(
        "--paste",
        action="store_true",
        help="do not listen for the browser: ask for the code claude.ai shows without echo, or read it from stdin",
    )
    json_option(login, returns_object(*ACCOUNT_KEYS, schema="Account"))
    login.set_defaults(func=cmd_account_oauth_login)


def _register_login(asub) -> None:
    login = asub.add_parser(
        "login",
        help="add a Claude account, or renew its token: runs `claude setup-token` and puts the token it prints on the "
        "hub, never on the screen",
    )
    login.add_argument("name", metavar="NAME", help="1 to 64 of a-z, 0-9, '.', '_' and '-'; an account's to renew it")
    login.add_argument(
        "--project",
        action="append",
        metavar="PROJECT",
        help="a project whose runs take it, on which you hold writer; repeat it for several (a new account needs one; "
        "renewing keeps the account's projects)",
    )
    login.add_argument(
        "--worker",
        action="append",
        metavar="NAME",
        help="only runs on this worker of yours take it; repeat it for several (default: any worker of yours; "
        "renewing keeps the account's workers)",
    )
    login.add_argument(
        "--expires",
        metavar="YYYY-MM-DD",
        help="no run takes it from 00:00 UTC of this day on (default: a year from today, less a day)",
    )
    login.add_argument(
        "--paste",
        action="store_true",
        help="do not run `claude setup-token`: ask for the token without echo, or read it from stdin",
    )
    json_option(login, returns_object(*ACCOUNT_KEYS, schema="Account"))
    login.set_defaults(func=cmd_account_login)


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
    _register_login(asub)
    _register_oauth_login(asub)

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
