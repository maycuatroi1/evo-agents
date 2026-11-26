"""``evo-agents hub email list|add|remove``: the emails of a member on the hub (``server.user_emails``), the addresses
the hub names that member by, such as the author of a commit.

Each command acts on the signed-in member's own emails; ``--user LOGIN`` acts on another member's, which a hub admin
alone may do. ``add`` and ``remove`` check the address before they ask the hub; ``remove`` finds the row of the
address in the member's list and removes it by its id, so the address never goes in a URL. ``--json`` prints the
hub's rows (``UserEmail``).

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Emails` is the
gateway to ``/v1/emails``, and :data:`EMAIL` their group. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
from urllib.parse import urlencode

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, Views, returns_array, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext, HubRoutes, when
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.user_emails import NOREPLY_DOMAIN, email_key, email_problem

EMAIL_KEYS = ("id", "email", "login", "created_at")
USER = Arg("--user", metavar="LOGIN", help="another member's emails (a hub admin only)")


class Emails(HubRoutes):
    """GET and POST /v1/emails (``?user=`` for another member's) and DELETE /v1/emails/{id} (Gateway)."""

    path = "/v1/emails"

    def __init__(self, hub: Hub, user: str | None):
        super().__init__(hub)
        self.user = user

    @classmethod
    def of(cls, ctx: HubContext) -> Emails:
        return cls(ctx.hub, ctx.args.user)

    def whose(self) -> str:
        return f"?{urlencode({'user': self.user})}" if self.user else ""

    def list(self) -> list[dict]:
        return self.call("GET", self.whose())

    def add(self, email: str) -> dict:
        return self.call("POST", self.whose(), {"email": email})

    def find(self, email: str) -> dict | None:
        """The row of ``email`` in the list, whatever its case."""
        return next((e for e in self.list() if email_key(e["email"]) == email_key(email)), None)

    def remove(self, row: int) -> dict:
        return self.call("DELETE", f"/{row}")


def checked(email: str) -> str:
    """``email`` without the spaces around it; UsageError when the hub would refuse it."""
    email = email.strip()
    problem = email_problem(email)
    if problem:
        raise UsageError(problem)
    return email


def whose(user: str | None) -> str:
    return user or "you"


class EmailList(HubCommand):
    name = "list"
    help = "the emails listed for you, or for --user"
    arguments = (USER,)
    output = returns_array(*EMAIL_KEYS, schema="UserEmail")

    def run(self, ctx: HubContext) -> Result:
        found = Emails.of(ctx).list()
        if found:
            listed = Table(
                ("ID", "EMAIL", "ADDED (UTC)"), [(e["id"], e["email"], when(e["created_at"])) for e in found]
            )
        else:
            listed = Text(f"No email listed for {whose(ctx.args.user)}.")
        noreply = Text(f"GitHub's noreply addresses (@{NOREPLY_DOMAIN}) name their login and need no row.")
        return Result(found, Views(listed, noreply))


class EmailAdd(HubCommand):
    name = "add"
    help = "list an email for you, or for --user; one member an address"
    arguments = (Arg("email", metavar="EMAIL", help="the address; case does not matter"), USER)
    output = returns_object(*EMAIL_KEYS, schema="UserEmail")

    def check(self, args: argparse.Namespace) -> None:
        checked(args.email)

    def run(self, ctx: HubContext) -> Result:
        added = Emails.of(ctx).add(checked(ctx.args.email))
        return Result(added, Text(f"{added['email']} is an email of {added['login']} (id {added['id']})."))


class EmailRemove(HubCommand):
    name = "remove"
    help = "drop one of your emails, or one of --user's"
    arguments = (Arg("email", metavar="EMAIL", help="the address, as `hub email list` shows it, any case"), USER)
    output = returns_object(*EMAIL_KEYS, schema="UserEmail")

    def check(self, args: argparse.Namespace) -> None:
        checked(args.email)

    def run(self, ctx: HubContext) -> Result:
        email, emails = checked(ctx.args.email), Emails.of(ctx)
        row = emails.find(email)
        if row is None:
            raise HubError(f"{email} is not an email of {whose(ctx.args.user)}: see `evo-agents hub email list`")
        removed = emails.remove(row["id"])
        return Result(removed, Text(f"Removed {removed['email']} from the emails of {removed['login']}."))


EMAIL = Group(
    "email",
    "your emails on the hub, which name you as a commit's author",
    [EmailList(), EmailAdd(), EmailRemove()],
)
