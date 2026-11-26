"""``evo-agents hub secret set|list|delete|bind|share|unshare``: the owner's secrets on the hub (``docs/credentials.md``).

``set`` writes a secret whole, value included (PUT /v1/secrets/{name}), creating it or replacing the one of that name.
The value never comes from the command line, where ``ps`` and the shell's history would see it: it is read from stdin
when stdin is not a terminal, one final line break dropped, else asked for with ``getpass``, which does not echo it.
The value is checked here for what the hub refuses anyway (empty, too long, a NUL, more than one line for kind git),
and never printed, not even in an error. Every check of the arguments comes before the value is read, so a mistyped
flag never costs a paste. ``--expires YYYY-MM-DD`` ends the secret at 00:00 UTC of that day, as GitLab ends an access
token on its expiry date. Kind ``forge`` is a GitLab token the hub alone uses to drive the merge requests of the
project's repos under ``--url-prefix``: it needs ``--expires`` and the admin role on each ``--project``, and takes no
``--username`` and no ``--worker``, as no run ever gets it. Without ``--project`` the secret is personal: the hub keeps
it for its owner alone and leases it to no run until it is bound to a project, by a ``set`` naming its projects or when
a run of its owner asks for its variable; it takes no ``--worker`` or ``--pool`` then, and kind forge always needs a
project.

``list`` prints the caller's own secrets without their values (GET /v1/secrets), and ``delete`` deletes one (DELETE
/v1/secrets/{name}): its value and bindings go, and the leases of it still out are revoked. ``bind``, in
``cli.secret.bindings``, binds one to other workers or pools without its value, and ``share`` and ``unshare``, in
``cli.secret.shares``, share one with other members. ``hub run credentials``, in ``evo_agents.hub.cli.run``, lists the
leases one run got of them. ``hub account``, in ``cli.account``, sees the env secrets as accounts.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes) over the gateway
``cli.secret.resources.Secrets``; :func:`secret_body` turns the options of ``set`` into the body to write, and
``SECRET`` is their group. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Group, Result, Table, Text, returns_array
from evo_agents.hub.cli.base import HubCommand, HubContext, when
from evo_agents.hub.cli.secret.bindings import (
    BOTH,
    SECRET_BIND,
    SECRET_COLUMNS,
    SECRET_KEYS,
    pool_arg,
    secret_row,
    target_text,
    where_text,
)
from evo_agents.hub.cli.secret.resources import Secrets
from evo_agents.hub.cli.secret.shares import SECRET_SHARE, SECRET_UNSHARE
from evo_agents.hub.cli.secret.values import Refused, check_name, check_value, expires_at, read_value
from evo_agents.hub.credentials import FORGE_KIND, SECRET_ENVIRONMENTS, SECRET_KINDS, env_name_refusal


def target(args) -> dict:
    """The fields of the secret's kind from ``args``; Refused for a flag of the other kind or one missing."""
    environment = getattr(args, "environment", None)
    if args.kind == "env":
        if args.url_prefix is not None or args.username is not None:
            raise Refused("--url-prefix and --username belong to --kind git, not env")
        if args.env_var is None:
            raise Refused("--kind env needs --env-var, the variable of the agent's environment it sets")
        refusal = env_name_refusal(args.env_var)
        if refusal is not None:
            raise Refused(f"--env-var {refusal}")
        return {"env_var": args.env_var, **({"environment": environment} if environment is not None else {})}
    for flag, given in (("--env-var", args.env_var), ("--environment", environment)):
        if given is not None:
            raise Refused(f"{flag} belongs to --kind env, not {args.kind}")
    if args.url_prefix is None:
        raise Refused(f"--kind {args.kind} needs --url-prefix, the https prefix of the origins it answers for")
    if args.kind == FORGE_KIND:
        if args.username is not None:
            raise Refused("--kind forge takes no --username: GitLab reads the token from a header")
        if args.worker or args.pool:
            raise Refused("--kind forge takes no --worker or --pool: only the hub uses it, and no run is leased it")
        if args.expires is None:
            raise Refused("--kind forge needs --expires, the day the GitLab token it holds ends")
        return {"url_prefix": args.url_prefix}
    return {"url_prefix": args.url_prefix, **({"username": args.username} if args.username is not None else {})}


def workers_text(secret: dict) -> str:
    if secret["kind"] == FORGE_KIND:
        return "used by the hub alone, never leased to a run"
    return where_text(secret)


def secret_body(args: argparse.Namespace) -> dict:
    """The body of PUT /v1/secrets/{name} for ``secret set``, but its value; Refused for options that cannot make
    one."""
    check_name(args.name)
    body = {"kind": args.kind, **target(args), "projects": args.project or []}
    if args.worker and args.pool:
        raise Refused(BOTH)
    if not args.project and args.kind == FORGE_KIND:
        raise Refused("--kind forge needs --project: the hub drives the merge requests of its projects with it")
    if not args.project and (args.worker or args.pool):
        raise Refused(
            "--worker and --pool need --project: a secret bound to no project is personal, and no run gets it"
        )
    body.update({key: given for key, given in (("workers", args.worker), ("pools", args.pool)) if given})
    if args.expires is not None:
        body["expires_at"] = expires_at(args.expires)
    return body


def written_text(written: dict) -> str:
    """What ``secret set`` prints of the secret the hub wrote."""
    verb = "Created" if written["created"] else "Replaced"
    ends = f"; it ends {when(written['expires_at'])} UTC" if written["expires_at"] else ""
    if written["projects"]:
        where = f"for project(s) {', '.join(written['projects'])}, {workers_text(written)}"
    else:
        where = "for you alone, bound to no project: no run gets it until it is bound to one"
    return (
        f"{verb} secret {written['name']} ({written['kind']}, {target_text(written)}) {where}{ends}. The hub keeps "
        "the value sealed and never shows it again."
    )


class SecretSet(HubCommand):
    name = "set"
    help = "create a secret, or replace it whole; the value comes from stdin, or is asked for without echo"
    arguments = (
        Arg("name", metavar="NAME", help="1 to 64 of a-z, 0-9, '.', '_' and '-'"),
        Arg(
            "--kind",
            required=True,
            choices=SECRET_KINDS,
            help="env: a variable of the agent's environment; git: what git's credential helper answers; forge: a "
            "GitLab token the hub alone uses to merge the project's merge requests, never leased to a run",
        ),
        Arg("--env-var", metavar="VAR", help="kind env: the variable it sets, such as CLAUDE_CODE_OAUTH_TOKEN"),
        Arg(
            "--url-prefix",
            metavar="URL",
            help="kinds git and forge: the https prefix of the origins it answers for, such as "
            "https://gitlab.example.org/group",
        ),
        Arg("--username", metavar="USER", help="kind git: the user git sends with the value (default oauth2)"),
        Arg(
            "--environment",
            choices=SECRET_ENVIRONMENTS,
            help="kind env: the environment it is for; a run whose principal's K7 is auto:test_secrets gets only the "
            "env secrets labelled test",
        ),
        Arg(
            "--project",
            action="append",
            metavar="PROJECT",
            help="a project whose runs get it, on which you hold writer (admin for kind forge); repeat it for "
            "several; left out, the secret is yours alone and no run gets it",
        ),
        Arg(
            "--worker",
            action="append",
            metavar="NAME",
            help="only runs on this worker of yours get it; repeat it for several (default: any worker of yours)",
        ),
        pool_arg(),
        Arg(
            "--expires",
            metavar="YYYY-MM-DD",
            help="no run gets it from 00:00 UTC of this day on (default: no end; kind forge needs one)",
        ),
    )

    def check(self, args: argparse.Namespace) -> None:
        secret_body(args)  # every check of the arguments before the value is read, so a mistyped flag costs no paste

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        secrets = Secrets.of(ctx)  # signed in before the value is asked for: not signed in costs no paste
        value = read_value(args.name)
        check_value(args.kind, value)
        written = secrets.put(args.name, {**secret_body(args), "value": value})
        return Result(written, Text(written_text(written)))


class SecretList(HubCommand):
    name = "list"
    help = "your secrets: kind, target, projects, workers and dates, never the value"
    output = returns_array(*SECRET_KEYS, schema="Secret")

    def run(self, ctx: HubContext) -> Result:
        found = Secrets.of(ctx).list()
        if ctx.args.json:
            return Result(found)
        empty = "You keep no secret on this hub; `evo-agents hub secret set` adds one."
        return Result(found, Table(SECRET_COLUMNS, [secret_row(s) for s in found], empty))


class SecretDelete(HubCommand):
    name = "delete"
    help = "delete a secret: its value and bindings go, and its leases still out are revoked"
    arguments = (Arg("name", metavar="NAME"),)

    def check(self, args: argparse.Namespace) -> None:
        check_name(args.name)

    def run(self, ctx: HubContext) -> Result:
        Secrets.of(ctx).delete(ctx.args.name)
        return Result(
            None,
            Text(
                f"Deleted secret {ctx.args.name}: its value and bindings are gone from the hub, and its leases still "
                "out are revoked. Revoke it where it was made as well."
            ),
        )


SECRET = Group(
    "secret",
    "your secrets on the hub, which runs of your projects get as leases; values are never shown",
    [SecretSet(), SecretList(), SecretDelete(), SECRET_BIND, SECRET_SHARE, SECRET_UNSHARE],
)
