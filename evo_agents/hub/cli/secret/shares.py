"""``evo-agents hub secret share|unshare`` and ``hub account share|unshare``: the owner shares a secret or an account
with a member, a unit of the org chart or everyone, for some of the projects it is bound to, and revokes a share
(``docs/credentials.md``; the rules are ``evo_agents.hub.credential_shares``).

``share NAME`` names the grantee with exactly one of ``--login``, ``--unit`` and ``--all``, and the projects with one
``--project`` or more (POST /v1/secrets/{name}/shares; an account is an env secret, so both nouns take the same route).
``--level``, ``--budget-usd``, ``--usage-ceiling``, ``--max-running`` and ``--expires YYYY-MM-DD`` (00:00 UTC of that
day, as for a secret) bound its use. A personal Claude subscription token is shared only with ``--ack-terms``: without
it the hub refuses with TERMS_WARNING, which the command prints whole on its own before exiting non-zero.
``unshare --share ID`` revokes a share (DELETE /v1/shares/{id}). ``hub secret list`` and ``hub account show`` list
the live shares of each. Standard library only, like the rest of the client.

The commands are :class:`Share` and :class:`Unshare`, classes of ``evo_agents.clikit`` made once for each noun
(``SECRET_SHARE``, ``ACCOUNT_SHARE`` and their ``unshare``), over the gateways ``cli.secret.resources.Secrets`` and
``Shares``.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime

from evo_agents.clikit import Arg, Result, Text, UsageError, returns_object
from evo_agents.hub.cli.base import EXIT_FAILED, HubCommand, HubContext
from evo_agents.hub.cli.secret.resources import Secrets, Shares
from evo_agents.hub.client import HubError
from evo_agents.hub.credential_shares import MAX_BUDGET_USD, MAX_RUNNING, TERMS_WARNING
from evo_agents.hub.org import LEVELS

DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The keys of a share in `hub secret share --json` and `hub account unshare --json` (`evo-agents hub contract print`).
SHARE_KEYS = (
    "id",
    "secret",
    "credential",
    "grantee_kind",
    "grantee",
    "projects",
    "level",
    "budget_usd",
    "usage_ceiling_pct",
    "max_running",
    "expires_at",
    "terms_ack_at",
    "created_by",
    "created_at",
    "revoked_at",
    "revoked_by",
    "run_id",
)


def share_expires_at(day: str) -> str | None:
    """``YYYY-MM-DD`` as the moment the share ends, 00:00 UTC of that day in ISO 8601; None when it is no day."""
    try:
        moment = datetime.strptime(day, "%Y-%m-%d") if DAY.fullmatch(day) else None
    except ValueError:
        return None
    return None if moment is None else moment.replace(tzinfo=UTC).isoformat()


def share_body(args) -> dict:
    """The body of POST /v1/secrets/{name}/shares from ``args``; UsageError saying what is wrong instead."""
    body: dict = {"projects": list(dict.fromkeys(args.project)), "terms_ack": bool(args.ack_terms)}
    if args.login is not None:
        body.update(grantee_kind="user", login=args.login)
    elif args.unit is not None:
        body.update(grantee_kind="unit", unit=args.unit)
    else:
        body["grantee_kind"] = "all"
    if args.budget_usd is not None and not 0 < args.budget_usd <= MAX_BUDGET_USD:
        raise UsageError(f"--budget-usd takes more than 0 and at most {MAX_BUDGET_USD}")
    if args.usage_ceiling is not None and not 1 <= args.usage_ceiling <= 100:
        raise UsageError("--usage-ceiling takes a percent from 1 to 100")
    if args.max_running is not None and not 1 <= args.max_running <= MAX_RUNNING:
        raise UsageError(f"--max-running takes 1 to {MAX_RUNNING}")
    limits = {
        "level": args.level,
        "budget_usd": args.budget_usd,
        "usage_ceiling_pct": args.usage_ceiling,
        "max_running": args.max_running,
    }
    body.update({key: value for key, value in limits.items() if value is not None})
    if args.expires is not None:
        ends = share_expires_at(args.expires)
        if ends is None:
            raise UsageError(f"--expires takes a day as YYYY-MM-DD, such as 2027-01-31, not {args.expires!r}")
        body["expires_at"] = ends
    return body


def _grantee_text(share: dict) -> str:
    if share["grantee_kind"] == "all":
        return "everyone on the hub"
    noun = "member" if share["grantee_kind"] == "user" else "unit"
    return f"{noun} {share['grantee']}"


class NotShared(Exception):
    """The hub refused to share a personal Claude subscription token without ``--ack-terms``."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


GRANTEES = (
    Arg("--login", metavar="LOGIN", help="share it with this member"),
    Arg("--unit", metavar="SLUG", help="share it with this unit of the org chart and every unit below"),
    Arg("--all", action="store_true", help="share it with everyone on the hub"),
)
LIMITS = (
    Arg("--level", choices=LEVELS, help="the autonomy level a run using it is held to"),
    Arg("--budget-usd", type=float, metavar="N", help="the most its runs may cost a month, in USD"),
    Arg(
        "--usage-ceiling", type=int, metavar="PCT", help="1 to 100: no run takes it while its usage is at this percent"
    ),
    Arg("--max-running", type=int, metavar="N", help="the most runs that may hold it at once"),
    Arg("--expires", metavar="YYYY-MM-DD", help="the share ends at 00:00 UTC of this day"),
    Arg(
        "--ack-terms",
        action="store_true",
        help="acknowledge the terms warning, which sharing a personal Claude subscription token needs",
    ),
)
SHARE_OUTPUT = returns_object(*SHARE_KEYS, schema="CredentialShare")


class Share(HubCommand):
    """``share`` under ``hub secret`` or ``hub account``, the noun it is made for."""

    name = "share"
    output = SHARE_OUTPUT

    def __init__(self, noun: str):
        self.noun = noun

    def attach(self, subparsers: argparse._SubParsersAction) -> None:
        help = f"share one of your {self.noun}s with a member, a unit or everyone, for some of its projects"
        self.configure(subparsers.add_parser(self.name, help=help))

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("name", metavar="NAME", help=f"one of your {self.noun}s")
        grantee = parser.add_mutually_exclusive_group(required=True)
        for arg in GRANTEES:
            grantee.add_argument(*arg.flags, **arg.options)
        parser.add_argument(
            "--project",
            required=True,
            action="append",
            metavar="PROJECT",
            help=f"a project the {self.noun} is bound to whose runs may use it; repeat it for several",
        )
        for arg in LIMITS:
            arg.add_to(parser)

    def check(self, args: argparse.Namespace) -> None:
        share_body(args)

    def run(self, ctx: HubContext) -> Result:
        name = ctx.args.name
        try:
            share = Secrets.of(ctx).share(name, share_body(ctx.args))
        except HubError as exc:
            if exc.status == 409 and TERMS_WARNING in str(exc):
                raise NotShared(name) from exc
            raise
        said = (
            f"Shared {self.noun} {share['secret']} with {_grantee_text(share)} for {', '.join(share['projects'])}: "
            f"share {share['id']}. `evo-agents hub {self.noun} unshare --share {share['id']}` revokes it."
        )
        return Result(share, Text(said))

    def failed(self, exc: Exception) -> int | None:
        if isinstance(exc, NotShared):
            print(TERMS_WARNING, file=sys.stderr)
            print(f"error: {exc.name} was not shared: give --ack-terms to share it anyway", file=sys.stderr)
            return EXIT_FAILED
        return super().failed(exc)


class Unshare(HubCommand):
    """``unshare`` under ``hub secret`` or ``hub account``."""

    name = "unshare"
    arguments = (Arg("--share", required=True, type=int, metavar="ID", help="the id of the share"),)
    output = SHARE_OUTPUT

    def __init__(self, noun: str):
        self.noun = noun

    def attach(self, subparsers: argparse._SubParsersAction) -> None:
        help = f"revoke a share of one of your {self.noun}s (an admin may revoke any)"
        self.configure(subparsers.add_parser(self.name, help=help))

    def check(self, args: argparse.Namespace) -> None:
        if args.share < 1:
            raise UsageError(f"--share takes the id of a share, a whole number from 1, not {args.share}")

    def run(self, ctx: HubContext) -> Result:
        share = Shares.of(ctx).revoke(ctx.args.share)
        return Result(
            share, Text(f"Revoked share {share['id']} of {share['secret']}: no new run of its grantee gets it.")
        )


SECRET_SHARE, SECRET_UNSHARE = Share("secret"), Unshare("secret")
ACCOUNT_SHARE, ACCOUNT_UNSHARE = Share("account"), Unshare("account")
