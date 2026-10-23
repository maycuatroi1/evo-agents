"""``evo-agents hub admin``, the parser of the hub admin's commands, and ``hub admin invite create|list|revoke``: the
one-time invite links of the hub (``evo_agents.hub.server.invites``).

``invite create --project P --role R --max-level L`` makes a link that gives whoever accepts it a grant on project P, as
``hub admin grant`` does; ``invite create --register`` makes one that gives the right to register one new project.
``--for LOGIN`` binds the link to that GitHub login alone, and ``--days`` sets how long it stays open (7 unless asked,
30 at most). The link, and its token, are printed this once: the hub keeps only a hash of the token. ``invite list``
prints every link with its status (open, accepted, revoked or expired), never a token; ``invite revoke ID`` revokes an
open link, or the unused right an accepted register link gave. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys

from evo_agents.hub.access import ROLES
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.contract import json_option, returns_array, returns_object

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


def _gives(invite: dict) -> str:
    if invite["kind"] == "register":
        used = f", used for {invite['project']}" if invite["project"] else ""
        return f"the right to register one new project{used}"
    return f"{invite['role']} on {invite['project']}, up to level {invite['max_level']}"


@client_command
def cmd_invite_create(args) -> int:
    join = (args.project, args.role, args.max_level)
    if not 1 <= args.days <= MAX_DAYS:
        print(f"error: a link stays open 1 to {MAX_DAYS} days, not {args.days}", file=sys.stderr)
        return 2
    if args.register and any(join):
        print("error: --register takes no --project, --role or --max-level", file=sys.stderr)
        return 2
    if not args.register and not all(join):
        print("error: an invite to a project needs --project, --role and --max-level", file=sys.stderr)
        return 2
    body = {"kind": "register" if args.register else "join", "for_login": args.for_login, "days": args.days}
    if not args.register:
        body |= {"project": args.project, "role": args.role, "max_level": args.max_level}
    hub, _ = signed_in()
    invite = hub.call("POST", "/v1/admin/invites", body)
    if args.json:
        print_json(invite)
        return 0
    bound = f", for {invite['for_login']} alone" if invite["for_login"] else ", for anyone signed in"
    print(f"Invite link {invite['id']}: {_gives(invite)}{bound}, open until {when(invite['expires_at'])} UTC.")
    print("Shown this once; the hub keeps only a hash of it:")
    print(f"  {invite['url'] or invite['token']}")
    if not invite["url"]:
        print("The hub has no EVO_HUB_PUBLIC_URL, so this is the token alone: it goes after /invite/ on the hub's web.")
    return 0


@client_command
def cmd_invite_list(args) -> int:
    hub, _ = signed_in()
    invites = hub.call("GET", "/v1/admin/invites")
    if args.json:
        print_json(invites)
        return 0
    rows = [
        (
            i["id"],
            i["status"],
            _gives(i),
            i["for_login"] or "anyone",
            i["created_by"],
            when(i["expires_at"]),
            i["accepted_by"] or "-",
        )
        for i in invites
    ]
    table(("ID", "STATUS", "GIVES", "FOR", "BY", "EXPIRES (UTC)", "ACCEPTED BY"), rows)
    return 0


@client_command
def cmd_invite_revoke(args) -> int:
    hub, _ = signed_in()
    invite = hub.call("POST", f"/v1/admin/invites/{args.id}/revoke")
    if args.json:
        print_json(invite)
        return 0
    print(f"Revoked invite link {invite['id']}: {_gives(invite)}.")
    return 0


def _invite_commands(asub) -> None:
    invite = asub.add_parser("invite", help="one-time invite links to a project, or to register one new project")
    isub = invite.add_subparsers(dest="invite_command", required=True)
    create = isub.add_parser("create", help="make an invite link; it and its token are printed this once")
    create.add_argument("--project", help="the project the link gives a grant on")
    create.add_argument("--role", choices=ROLES, help="the role it gives on --project")
    create.add_argument("--max-level", help="the highest label level of --project it reaches")
    create.add_argument(
        "--register", action="store_true", help="give the right to register one new project instead of a grant"
    )
    create.add_argument(
        "--for", "--for-login", dest="for_login", metavar="LOGIN", help="the one GitHub login that may accept it"
    )
    create.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help=f"days the link stays open, 1 to {MAX_DAYS} (default: 7)"
    )
    json_option(create, returns_object(*CREATED_KEYS, schema="InviteCreated"))
    create.set_defaults(func=cmd_invite_create)
    invite_list = isub.add_parser("list", help="every invite link with its status; never a token")
    json_option(invite_list, returns_array(*INVITE_KEYS, schema="InviteRow"))
    invite_list.set_defaults(func=cmd_invite_list)
    revoke = isub.add_parser("revoke", help="revoke an open link, or the unused right of an accepted register link")
    revoke.add_argument("id", metavar="ID", type=int, help="the link's id, from `hub admin invite list`")
    json_option(revoke, returns_object(*INVITE_KEYS, schema="InviteRow"))
    revoke.set_defaults(func=cmd_invite_revoke)


def admin_commands(hsub):
    """The ``hub admin`` parser with ``invite`` added; the subparsers ``register_client`` adds the other commands to."""
    admin = hsub.add_parser("admin", help="users, grants and invite links (hub admins only)")
    asub = admin.add_subparsers(dest="admin_command", required=True)
    _invite_commands(asub)
    return asub
