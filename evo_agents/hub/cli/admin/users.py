"""``evo-agents hub admin grant|revoke|users|stats``: a hub admin's grants, the users of the hub and the rows of its
tables (``evo_agents.hub.server.admin``).

``grant LOGIN PROJECT --role R --max-level L`` gives a login a role on a project, or changes it, and ``revoke LOGIN
PROJECT`` takes it away. ``users`` lists every user with the admin flag, the last sign-in, the tokens and the grants;
``stats`` counts the rows in every table. The commands are classes of ``evo_agents.clikit`` over the gateway
``cli.admin.resources.HubAdmin``. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Table, Text, returns_array, returns_map
from evo_agents.hub.access import ROLES
from evo_agents.hub.cli.admin.resources import HubAdmin
from evo_agents.hub.cli.base import HubCommand, HubContext, when

USER_KEYS = ("login", "admin", "signed_in", "created_at", "last_seen_at", "active_tokens", "grants")


def grants_text(grants: list[dict]) -> str:
    """A user's grants on one line; ``none`` when there are none."""
    return ", ".join(f"{g['project']} {g['role']} up to {g['max_level']}" for g in grants) or "none"


class AdminGrant(HubCommand):
    name = "grant"
    help = "give a login a role on a project, or change it"
    arguments = (
        Arg("login", metavar="LOGIN", help="GitHub login; it may not have signed in yet"),
        Arg("project", metavar="PROJECT", help="project name on the hub"),
        Arg("--role", required=True, choices=ROLES),
        Arg("--max-level", required=True, help="highest label level of the project the login may see"),
    )

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        grant = HubAdmin(ctx.hub).grant(args.project, args.login, {"role": args.role, "max_level": args.max_level})
        verb = "Granted" if grant["created"] else "Changed the grant:"
        said = f"{verb} {grant['login']} {grant['role']} on {grant['project']}, up to level {grant['max_level']}."
        return Result(grant, Text(said))


class AdminRevoke(HubCommand):
    name = "revoke"
    help = "take away the role a login has on a project"
    arguments = (Arg("login", metavar="LOGIN"), Arg("project", metavar="PROJECT"))

    def run(self, ctx: HubContext) -> Result:
        HubAdmin(ctx.hub).revoke_grant(ctx.args.project, ctx.args.login)
        return Result(None, Text(f"Revoked the grant of {ctx.args.login} on {ctx.args.project}."))


class AdminUsers(HubCommand):
    name = "users"
    help = "every user with admin flag, last sign-in, tokens and grants"
    output = returns_array(*USER_KEYS, schema="UserRow")
    columns = ("LOGIN", "ADMIN", "SIGNED IN", "LAST SEEN (UTC)", "TOKENS", "GRANTS")

    def run(self, ctx: HubContext) -> Result:
        users = HubAdmin(ctx.hub).users()
        return Result(users, Table(self.columns, [self.row(user) for user in users], bare=True))

    @staticmethod
    def row(user: dict) -> tuple:
        return (
            user["login"],
            "yes" if user["admin"] else "no",
            "yes" if user["signed_in"] else "not yet",
            when(user["last_seen_at"]),
            user["active_tokens"],
            grants_text(user["grants"]),
        )


class AdminStats(HubCommand):
    name = "stats"
    help = "rows in every hub table"
    output = returns_map()

    def run(self, ctx: HubContext) -> Result:
        counts = HubAdmin(ctx.hub).stats()
        return Result(counts, Table(("TABLE", "ROWS"), sorted(counts.items()), bare=True))


GRANT, REVOKE, USERS, STATS = AdminGrant(), AdminRevoke(), AdminUsers(), AdminStats()
