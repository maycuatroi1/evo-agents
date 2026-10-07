"""``evo-agents hub secret set|list|delete``: the owner's secrets on the hub (``docs/credentials.md``).

``set`` writes a secret whole, value included (PUT /v1/secrets/{name}), creating it or replacing the one of that name.
The value never comes from the command line, where ``ps`` and the shell's history would see it: it is read from stdin
when stdin is not a terminal, one final line break dropped, else asked for with ``getpass``, which does not echo it.
The value is checked here for what the hub refuses anyway (empty, too long, a NUL, more than one line for kind git),
and never printed, not even in an error. Every check of the arguments comes before the value is read, so a mistyped
flag never costs a paste. ``--expires YYYY-MM-DD`` ends the secret at 00:00 UTC of that day, as GitLab ends an access
token on its expiry date.

``list`` prints the caller's own secrets without their values (GET /v1/secrets), and ``delete`` deletes one (DELETE
/v1/secrets/{name}): its value and bindings go, and the leases of it still out are revoked. ``hub run credentials``,
in ``run_cli``, lists the leases one run got of them. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import getpass
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in, _table, _when
from evo_agents.hub.contract import json_option, returns_array
from evo_agents.hub.credentials import MAX_SECRET_BYTES, SECRET_KINDS, SECRET_NAME, env_name_refusal

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
    return moment.replace(tzinfo=timezone.utc).isoformat()


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
