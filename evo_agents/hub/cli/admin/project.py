"""``evo-agents hub admin project show|self-managed|grant|revoke|invite``: a self-managed project
(``evo_agents.hub.server.project_admin``, plan company-ops-on-hub).

``show PROJECT`` prints whether the project is self-managed, the GitHub owners its admins install plugins from, its
ladder and every grant; a hub admin or an admin of the project reads it. ``self-managed PROJECT --on --owner OWNER`` (a
hub admin; ``--owner`` repeats) turns the state on, ``--off`` turns it off. In a self-managed project its admins give and
take back grants (``grant LOGIN PROJECT``, ``revoke LOGIN PROJECT``) and make invite links (``invite PROJECT``) with the
role and max level of their own at most; ``hub plugin install --github OWNER/REPO --project PROJECT`` and
``hub plugin remove NAME --project PROJECT`` install and remove a plugin of a trusted owner for that project alone.
Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub.access import ROLES
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.cli_invites import CREATED_KEYS, DEFAULT_DAYS, MAX_DAYS
from evo_agents.hub.contract import json_option, returns_object

VIEW_KEYS = ("name", "self_managed", "trusted_owners", "levels", "grants")
GRANT_KEYS = ("project", "login", "role", "max_level", "created")


def _path(project: str, *rest: str) -> str:
    return "/".join(("/v1/projects", quote(project, safe=""), *rest))


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    view = hub.call("GET", _path(args.project, "admin"))
    if args.json:
        print_json(view)
        return 0
    if view["self_managed"]:
        print(
            f"Project {view['name']} is self-managed; its admins install plugins from: {', '.join(view['trusted_owners'])}."
        )
    else:
        print(f"Project {view['name']} is not self-managed: only a hub admin installs plugins and gives grants.")
    print(f"levels: {', '.join(view['levels'])}")
    rows = [
        (g["login"], g["role"], g["max_level"], g["granted_by"] or "-", when(g["granted_at"])) for g in view["grants"]
    ]
    table(("LOGIN", "ROLE", "MAX LEVEL", "BY", "SINCE (UTC)"), rows)
    return 0


@client_command
def cmd_self_managed(args) -> int:
    if args.on == args.off:
        print("error: pass --on with --owner OWNER, or --off", file=sys.stderr)
        return 2
    if args.on and not args.owner:
        print("error: --on needs at least one --owner OWNER, a GitHub user or organisation", file=sys.stderr)
        return 2
    if args.off and args.owner:
        print("error: --off takes no --owner", file=sys.stderr)
        return 2
    hub, _ = signed_in()
    body = {"enabled": args.on, "trusted_owners": args.owner or []}
    view = hub.call("PUT", f"/v1/admin/projects/{quote(args.project, safe='')}/self-managed", body)
    if args.json:
        print_json(view)
    elif view["self_managed"]:
        print(
            f"Project {view['name']} is self-managed now; its admins install plugins from: {', '.join(view['trusted_owners'])}."
        )
    else:
        print(f"Project {view['name']} is no longer self-managed; its plugins and grants stay as they are.")
    return 0


@client_command
def cmd_grant(args) -> int:
    hub, _ = signed_in()
    grant = hub.call(
        "PUT",
        _path(args.project, "admin", "grants", quote(args.login, safe="")),
        {"role": args.role, "max_level": args.max_level},
    )
    if args.json:
        print_json(grant)
        return 0
    verb = "Granted" if grant["created"] else "Changed the grant:"
    print(f"{verb} {grant['login']} {grant['role']} on {grant['project']}, up to level {grant['max_level']}.")
    return 0


@client_command
def cmd_revoke(args) -> int:
    hub, _ = signed_in()
    hub.call("DELETE", _path(args.project, "admin", "grants", quote(args.login, safe="")))
    print(f"Revoked the grant of {args.login} on {args.project}.")
    return 0


@client_command
def cmd_invite(args) -> int:
    if not 1 <= args.days <= MAX_DAYS:
        print(f"error: a link stays open 1 to {MAX_DAYS} days, not {args.days}", file=sys.stderr)
        return 2
    hub, _ = signed_in()
    body = {"role": args.role, "max_level": args.max_level, "for_login": args.for_login, "days": args.days}
    invite = hub.call("POST", _path(args.project, "admin", "invites"), body)
    if args.json:
        print_json(invite)
        return 0
    bound = f"for {invite['for_login']} alone" if invite["for_login"] else "for anyone signed in"
    print(
        f"Invite link {invite['id']}: {invite['role']} on {invite['project']}, up to level {invite['max_level']}, {bound}."
    )
    print("Shown this once; the hub keeps only a hash of it:")
    print(f"  {invite['url'] or invite['token']}")
    return 0


def project_commands(asub) -> None:
    """Add ``project`` to the ``hub admin`` subparsers ``asub``."""
    project = asub.add_parser("project", help="a project's self-managed state, and its admins' grants and invite links")
    psub = project.add_subparsers(dest="project_command", required=True)
    show = psub.add_parser("show", help="self-managed or not, trusted owners, levels and grants of a project")
    show.add_argument("project", metavar="PROJECT")
    json_option(show, returns_object(*VIEW_KEYS, schema="ProjectAdminView"))
    show.set_defaults(func=cmd_show)
    managed = psub.add_parser("self-managed", help="turn a project's self-managed state on or off (hub admins only)")
    managed.add_argument("project", metavar="PROJECT")
    managed.add_argument("--on", action="store_true", help="its admins install plugins and give grants themselves")
    managed.add_argument("--off", action="store_true", help="back to a hub admin alone")
    managed.add_argument(
        "--owner", action="append", metavar="OWNER", help="with --on: a GitHub owner whose releases they install"
    )
    json_option(managed, returns_object(*VIEW_KEYS, schema="ProjectAdminView"))
    managed.set_defaults(func=cmd_self_managed)
    grant = psub.add_parser("grant", help="give a login a role on a self-managed project, or change it")
    grant.add_argument("login", metavar="LOGIN")
    grant.add_argument("project", metavar="PROJECT")
    grant.add_argument("--role", required=True, choices=ROLES)
    grant.add_argument("--max-level", required=True, help="highest label level, at most your own")
    json_option(grant, returns_object(*GRANT_KEYS, schema="Grant"))
    grant.set_defaults(func=cmd_grant)
    revoke = psub.add_parser("revoke", help="take back a login's grant on a self-managed project")
    revoke.add_argument("login", metavar="LOGIN")
    revoke.add_argument("project", metavar="PROJECT")
    revoke.set_defaults(func=cmd_revoke)
    invite = psub.add_parser("invite", help="make a one-time link that joins a self-managed project")
    invite.add_argument("project", metavar="PROJECT")
    invite.add_argument("--role", required=True, choices=ROLES)
    invite.add_argument("--max-level", required=True, help="highest label level, at most your own")
    invite.add_argument(
        "--for", "--for-login", dest="for_login", metavar="LOGIN", help="the one login that may accept it"
    )
    invite.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help=f"days the link stays open, 1 to {MAX_DAYS} (default: 7)"
    )
    json_option(invite, returns_object(*CREATED_KEYS, schema="InviteCreated"))
    invite.set_defaults(func=cmd_invite)
