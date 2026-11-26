"""``evo-agents hub admin invite create|list|revoke``: the one-time invite links of the hub
(``evo_agents.hub.server.invites``).

``invite create --project P --role R --max-level L`` makes a link that gives whoever accepts it a grant on project P, as
``hub admin grant`` does; ``invite create --register`` makes one that gives the right to register one new project.
``--for LOGIN`` binds the link to that GitHub login alone, and ``--days`` sets how long it stays open (7 unless asked,
30 at most). The link, and its token, are printed this once: the hub keeps only a hash of the token. ``invite list``
prints every link with its status (open, accepted, revoked or expired), never a token; ``invite revoke ID`` revokes an
open link, or the unused right an accepted register link gave.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes), over the gateway
``cli.admin.resources.HubAdmin``; :data:`INVITE` is their group, which ``hub admin`` lists. Standard library only, like
the rest of the client.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, returns_array, returns_object
from evo_agents.hub.access import ROLES
from evo_agents.hub.cli.admin.resources import HubAdmin
from evo_agents.hub.cli.base import HubCommand, HubContext, when

# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
INVITE_KEYS = (
    "id",
    "kind",
    "project",
    "role",
    "max_level",
    "for_login",
    "created_by",
    "created_at",
    "expires_at",
    "status",
    "accepted_by",
    "accepted_at",
    "used_at",
)
CREATED_KEYS = (*INVITE_KEYS, "token", "url")
DEFAULT_DAYS = 7
MAX_DAYS = 30
FOR_LOGIN_FLAGS = ("--for", "--for-login")
DAYS = Arg("--days", type=int, default=DEFAULT_DAYS, help=f"days the link stays open, 1 to {MAX_DAYS} (default: 7)")


def check_days(days: int) -> None:
    """UsageError when a link would stay open for ``days`` the hub does not allow."""
    if not 1 <= days <= MAX_DAYS:
        raise UsageError(f"a link stays open 1 to {MAX_DAYS} days, not {days}")


def gives(invite: dict) -> str:
    """What accepting ``invite`` gives, in words."""
    if invite["kind"] == "register":
        used = f", used for {invite['project']}" if invite["project"] else ""
        return f"the right to register one new project{used}"
    return f"{invite['role']} on {invite['project']}, up to level {invite['max_level']}"


def invite_body(args: argparse.Namespace) -> dict:
    """The body of POST /v1/admin/invites for ``invite create``; UsageError for options that do not go together."""
    check_days(args.days)
    join = (args.project, args.role, args.max_level)
    if args.register and any(join):
        raise UsageError("--register takes no --project, --role or --max-level")
    if not args.register and not all(join):
        raise UsageError("an invite to a project needs --project, --role and --max-level")
    body = {"kind": "register" if args.register else "join", "for_login": args.for_login, "days": args.days}
    if not args.register:
        body |= {"project": args.project, "role": args.role, "max_level": args.max_level}
    return body


def created_lines(invite: dict) -> list[str]:
    """What ``invite create`` prints of the link the hub made."""
    bound = f", for {invite['for_login']} alone" if invite["for_login"] else ", for anyone signed in"
    lines = [
        f"Invite link {invite['id']}: {gives(invite)}{bound}, open until {when(invite['expires_at'])} UTC.",
        "Shown this once; the hub keeps only a hash of it:",
        f"  {invite['url'] or invite['token']}",
    ]
    if not invite["url"]:
        lines.append(
            "The hub has no EVO_HUB_PUBLIC_URL, so this is the token alone: it goes after /invite/ on the hub's web."
        )
    return lines


class InviteCreate(HubCommand):
    name = "create"
    help = "make an invite link; it and its token are printed this once"
    arguments = (
        Arg("--project", help="the project the link gives a grant on"),
        Arg("--role", choices=ROLES, help="the role it gives on --project"),
        Arg("--max-level", help="the highest label level of --project it reaches"),
        Arg("--register", action="store_true", help="give the right to register one new project instead of a grant"),
        Arg(*FOR_LOGIN_FLAGS, dest="for_login", metavar="LOGIN", help="the one GitHub login that may accept it"),
        DAYS,
    )
    output = returns_object(*CREATED_KEYS, schema="InviteCreated")

    def check(self, args: argparse.Namespace) -> None:
        invite_body(args)

    def run(self, ctx: HubContext) -> Result:
        invite = HubAdmin(ctx.hub).create_invite(invite_body(ctx.args))
        return Result(invite, Text(*created_lines(invite)))


class InviteList(HubCommand):
    name = "list"
    help = "every invite link with its status; never a token"
    output = returns_array(*INVITE_KEYS, schema="InviteRow")
    columns = ("ID", "STATUS", "GIVES", "FOR", "BY", "EXPIRES (UTC)", "ACCEPTED BY")

    def run(self, ctx: HubContext) -> Result:
        invites = HubAdmin(ctx.hub).invites()
        return Result(invites, Table(self.columns, [self.row(invite) for invite in invites], bare=True))

    @staticmethod
    def row(invite: dict) -> tuple:
        return (
            invite["id"],
            invite["status"],
            gives(invite),
            invite["for_login"] or "anyone",
            invite["created_by"],
            when(invite["expires_at"]),
            invite["accepted_by"] or "-",
        )


class InviteRevoke(HubCommand):
    name = "revoke"
    help = "revoke an open link, or the unused right of an accepted register link"
    arguments = (Arg("id", metavar="ID", type=int, help="the link's id, from `hub admin invite list`"),)
    output = returns_object(*INVITE_KEYS, schema="InviteRow")

    def run(self, ctx: HubContext) -> Result:
        invite = HubAdmin(ctx.hub).revoke_invite(ctx.args.id)
        return Result(invite, Text(f"Revoked invite link {invite['id']}: {gives(invite)}."))


INVITE = Group(
    "invite",
    "one-time invite links to a project, or to register one new project",
    [InviteCreate(), InviteList(), InviteRevoke()],
)
