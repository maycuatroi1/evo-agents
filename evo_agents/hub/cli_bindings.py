"""Where a secret or an account is bound, for ``hub secret`` and ``hub account`` (``cli_secrets``): on workers named
(``--worker``), on the nodes of worker pools (``--pool``, plan worker-pools, criterion a4), or on any worker of its
owner. A pool covers each of its nodes, those that join after the binding too; a binding names workers or pools, not
both.

``secret bind NAME`` binds one of the caller's secrets again without its value (PATCH /v1/secrets/{name}): to other
workers of theirs (``--worker``, repeatable), to the nodes of their worker pools (``--pool``, repeatable) or to any
worker of theirs (``--anywhere``), exactly one of the three. Its kind, target, projects, end and shares stay. It prints
the secret as ``secret list`` shows a row of it (``SECRET_COLUMNS``, ``secret_row``), or the hub's answer with
``--json``."""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.credentials import FORGE_KIND, SECRET_NAME

EXIT_USAGE = 2
BOTH = "give --worker or --pool, not both: a pool covers each of its nodes already"
# The keys of a secret in `hub secret list --json` and `hub secret bind --json` (the contract, `evo-agents hub contract
# print`).
SECRET_KEYS = (
    "name",
    "kind",
    "env_var",
    "url_prefix",
    "username",
    "environment",
    "projects",
    "workers",
    "pools",
    "expires_at",
    "created_at",
    "updated_at",
    "shares",
)
# The columns of `hub secret list`, a row each secret (`secret_row`).
SECRET_COLUMNS = ("NAME", "KIND", "TARGET", "PROJECTS", "WORKERS", "EXPIRES (UTC)", "UPDATED (UTC)")


def add_pool_flag(parser, runs: str = "get") -> None:
    """``--pool NAME``, repeatable, on ``parser``; ``runs`` is the verb of the help ("get" or "take")."""
    parser.add_argument(
        "--pool",
        action="append",
        metavar="NAME",
        help=f"only runs on a node of this worker pool of yours {runs} it, those that join later too; repeat it for "
        "several; not with --worker",
    )


def where_text(item: dict) -> str:
    """Where ``item``, a secret or an account of the API, is bound: on worker a, b, on pool ci, or on any worker of yours."""
    if item.get("pools"):
        return "on pool " + ", ".join(item["pools"])
    return "on " + (", ".join(item["workers"]) if item["workers"] else "any worker of yours")


def where_cell(item: dict) -> str:
    """The WORKERS cell of a listing: the workers, pool <name> for each pool, or any."""
    return ", ".join([*item["workers"], *(f"pool {name}" for name in item.get("pools") or ())]) or "any"


def bound_to(args, existing: dict | None) -> dict:
    """The workers and pools of a login's body: those given, else the account's when it is one already. ``str`` of
    the ValueError raised for both says why."""
    if args.worker and args.pool:
        raise ValueError(BOTH)
    if args.worker is not None or args.pool is not None:
        return {"workers": args.worker or [], "pools": args.pool or []}
    if existing is None:
        return {"workers": [], "pools": []}
    return {"workers": existing["workers"], "pools": existing.get("pools") or []}


# A secret as `hub secret list` shows it


def secret_path(name: str) -> str:
    return f"/v1/secrets/{quote(name, safe='')}"


def target_text(secret: dict) -> str:
    """What the secret answers for: its variable (and the environment it is labelled for), or its URL prefix."""
    if secret["kind"] == "env":
        labelled = f" ({secret['environment']})" if secret.get("environment") else ""
        return (secret["env_var"] or "-") + labelled
    if secret["kind"] == FORGE_KIND:
        return secret["url_prefix"] or "-"
    return f"{secret['url_prefix']} as {secret['username']}"


def secret_row(secret: dict) -> tuple:
    """The row of ``secret``, as the API gives it, under SECRET_COLUMNS."""
    return (
        secret["name"],
        secret["kind"],
        target_text(secret),
        ", ".join(secret["projects"]) or "-",
        "-" if secret["kind"] == FORGE_KIND or not secret["projects"] else where_cell(secret),
        when(secret["expires_at"]),
        when(secret["updated_at"]),
    )


# `hub secret bind`


def rebinding(args) -> dict:
    """The body of PATCH /v1/secrets/{name}: the workers or the pools given, or no worker for ``--anywhere``."""
    if args.pool:
        return {"pools": args.pool}
    return {"workers": args.worker or []}


@client_command
def cmd_bind(args) -> int:
    if not SECRET_NAME.fullmatch(args.name):
        print(f"error: {args.name!r} is not a secret's name", file=sys.stderr)
        return EXIT_USAGE
    hub, _ = signed_in()
    secret = hub.call("PATCH", secret_path(args.name), rebinding(args))
    if args.json:
        print_json(secret)
    else:
        table(SECRET_COLUMNS, [secret_row(secret)])
    return 0


def register_bind(ssub) -> None:
    """``hub secret bind`` on ``ssub``, the subcommands of ``hub secret``."""
    bind = ssub.add_parser(
        "bind",
        help="bind a secret to other workers or pools of yours, or to any worker; its value and the rest stay",
    )
    bind.add_argument("name", metavar="NAME")
    where = bind.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--worker",
        action="append",
        metavar="NAME",
        help="only runs on this worker of yours get it; repeat it for several; not with --pool",
    )
    add_pool_flag(where)
    where.add_argument("--anywhere", action="store_true", help="clear its binding: runs on any worker of yours get it")
    json_option(bind, returns_object(*SECRET_KEYS, schema="Secret"))
    bind.set_defaults(func=cmd_bind)
