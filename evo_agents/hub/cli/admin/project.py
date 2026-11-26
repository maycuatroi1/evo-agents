"""``evo-agents hub admin project show|self-managed|grant|revoke|invite``: a self-managed project
(``evo_agents.hub.server.project_admin``, plan company-ops-on-hub).

``show PROJECT`` prints whether the project is self-managed, the GitHub owners its admins install plugins from, its
ladder and every grant; a hub admin or an admin of the project reads it. ``self-managed PROJECT --on --owner OWNER`` (a
hub admin; ``--owner`` repeats) turns the state on, ``--off`` turns it off. In a self-managed project its admins give and
take back grants (``grant LOGIN PROJECT``, ``revoke LOGIN PROJECT``) and make invite links (``invite PROJECT``) with the
role and max level of their own at most; ``hub plugin install --github OWNER/REPO --project PROJECT`` and
``hub plugin remove NAME --project PROJECT`` install and remove a plugin of a trusted owner for that project alone.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes), over the gateways
``cli.admin.resources.ProjectAdmin`` and ``HubAdmin`` (the self-managed state is the hub admin's); :data:`PROJECT` is
their group, which ``hub admin`` lists. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, Views, returns_object
from evo_agents.hub.access import ROLES
from evo_agents.hub.cli.admin.invites import CREATED_KEYS, DAYS, FOR_LOGIN_FLAGS, check_days
from evo_agents.hub.cli.admin.resources import HubAdmin, ProjectAdmin
from evo_agents.hub.cli.base import HubCommand, HubContext, when

VIEW_KEYS = ("name", "self_managed", "trusted_owners", "levels", "grants")
GRANT_KEYS = ("project", "login", "role", "max_level", "created")
PROJECT_NAME = Arg("project", metavar="PROJECT")
LOGIN = Arg("login", metavar="LOGIN")
ROLE = Arg("--role", required=True, choices=ROLES)
MAX_LEVEL = Arg("--max-level", required=True, help="highest label level, at most your own")


def admin_of(ctx: HubContext) -> ProjectAdmin:
    return ProjectAdmin(ctx.hub, ctx.args.project)


def check_state(args: argparse.Namespace) -> None:
    """UsageError unless ``self-managed`` has --on with an --owner, or --off alone."""
    if args.on == args.off:
        raise UsageError("pass --on with --owner OWNER, or --off")
    if args.on and not args.owner:
        raise UsageError("--on needs at least one --owner OWNER, a GitHub user or organisation")
    if args.off and args.owner:
        raise UsageError("--off takes no --owner")


class Show(HubCommand):
    name = "show"
    help = "self-managed or not, trusted owners, levels and grants of a project"
    arguments = (PROJECT_NAME,)
    output = returns_object(*VIEW_KEYS, schema="ProjectAdminView")
    columns = ("LOGIN", "ROLE", "MAX LEVEL", "BY", "SINCE (UTC)")

    def run(self, ctx: HubContext) -> Result:
        view = admin_of(ctx).view()
        if view["self_managed"]:
            state = (
                f"Project {view['name']} is self-managed; its admins install plugins from: "
                f"{', '.join(view['trusted_owners'])}."
            )
        else:
            state = f"Project {view['name']} is not self-managed: only a hub admin installs plugins and gives grants."
        rows = [
            (g["login"], g["role"], g["max_level"], g["granted_by"] or "-", when(g["granted_at"]))
            for g in view["grants"]
        ]
        lines = Text(state, f"levels: {', '.join(view['levels'])}")
        return Result(view, Views(lines, Table(self.columns, rows, bare=True)))


class SelfManaged(HubCommand):
    name = "self-managed"
    help = "turn a project's self-managed state on or off (hub admins only)"
    arguments = (
        PROJECT_NAME,
        Arg("--on", action="store_true", help="its admins install plugins and give grants themselves"),
        Arg("--off", action="store_true", help="back to a hub admin alone"),
        Arg("--owner", action="append", metavar="OWNER", help="with --on: a GitHub owner whose releases they install"),
    )
    output = returns_object(*VIEW_KEYS, schema="ProjectAdminView")

    def check(self, args: argparse.Namespace) -> None:
        check_state(args)

    def run(self, ctx: HubContext) -> Result:
        body = {"enabled": ctx.args.on, "trusted_owners": ctx.args.owner or []}
        view = HubAdmin(ctx.hub).self_managed(ctx.args.project, body)
        if view["self_managed"]:
            said = (
                f"Project {view['name']} is self-managed now; its admins install plugins from: "
                f"{', '.join(view['trusted_owners'])}."
            )
        else:
            said = f"Project {view['name']} is no longer self-managed; its plugins and grants stay as they are."
        return Result(view, Text(said))


class Grant(HubCommand):
    name = "grant"
    help = "give a login a role on a self-managed project, or change it"
    arguments = (LOGIN, PROJECT_NAME, ROLE, MAX_LEVEL)
    output = returns_object(*GRANT_KEYS, schema="Grant")

    def run(self, ctx: HubContext) -> Result:
        body = {"role": ctx.args.role, "max_level": ctx.args.max_level}
        grant = admin_of(ctx).grant(ctx.args.login, body)
        verb = "Granted" if grant["created"] else "Changed the grant:"
        said = f"{verb} {grant['login']} {grant['role']} on {grant['project']}, up to level {grant['max_level']}."
        return Result(grant, Text(said))


class Revoke(HubCommand):
    name = "revoke"
    help = "take back a login's grant on a self-managed project"
    arguments = (LOGIN, PROJECT_NAME)

    def run(self, ctx: HubContext) -> Result:
        admin_of(ctx).revoke(ctx.args.login)
        return Result(None, Text(f"Revoked the grant of {ctx.args.login} on {ctx.args.project}."))


class Invite(HubCommand):
    name = "invite"
    help = "make a one-time link that joins a self-managed project"
    arguments = (
        PROJECT_NAME,
        ROLE,
        MAX_LEVEL,
        Arg(*FOR_LOGIN_FLAGS, dest="for_login", metavar="LOGIN", help="the one login that may accept it"),
        DAYS,
    )
    output = returns_object(*CREATED_KEYS, schema="InviteCreated")

    def check(self, args: argparse.Namespace) -> None:
        check_days(args.days)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"role": args.role, "max_level": args.max_level, "for_login": args.for_login, "days": args.days}
        invite = admin_of(ctx).invite(body)
        bound = f"for {invite['for_login']} alone" if invite["for_login"] else "for anyone signed in"
        return Result(
            invite,
            Text(
                f"Invite link {invite['id']}: {invite['role']} on {invite['project']}, up to level "
                f"{invite['max_level']}, {bound}.",
                "Shown this once; the hub keeps only a hash of it:",
                f"  {invite['url'] or invite['token']}",
            ),
        )


PROJECT = Group(
    "project",
    "a project's self-managed state, and its admins' grants and invite links",
    [Show(), SelfManaged(), Grant(), Revoke(), Invite()],
)
