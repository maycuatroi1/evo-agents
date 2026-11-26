"""``evo-agents hub login``, ``logout`` and ``whoami``: signing this machine in to a hub and out of it.

``login`` signs in with GitHub (device flow) and keeps a machine token; ``logout`` revokes it on the hub and deletes it
here (``--force`` deletes it when the hub cannot be reached); ``whoami`` prints the signed-in login, whether it is an
admin, and its grants. The rest of the client side of the hub lives in its own modules, at the path of each command:
``hub token`` in ``cli.token``, ``hub admin`` in ``cli.admin``, ``hub project`` in ``cli.project`` and ``hub
registry`` in ``cli.registry``.

Standard library only, like ``client``. Results go to stdout; a failure is one ``error:`` line on stderr and the exit
status ``client.exit_status`` gives it: 69 when the hub or the network could not be reached, 75 when the hub answered
5xx or 429, 1 otherwise (2 is argparse's, for a wrong command line). A 401 says to run ``evo-agents hub login``.
``--json`` prints what the hub answered, with the keys declared next to the flag (``contract.json_option``). No command
ever prints a token.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes), over the gateway
``cli.token.Account`` (``/v1/auth/...``). :data:`SESSION` is the node ``evo_agents.hub.cli`` attaches: these three
commands and the groups ``token``, ``admin``, ``project`` and ``registry``, one after the other in that order.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from evo_agents.clikit import Arg, Node, Result, Text, returns_object
from evo_agents.hub.cli.admin import ADMIN
from evo_agents.hub.cli.admin.users import grants_text
from evo_agents.hub.cli.base import HubCommand, HubContext, when
from evo_agents.hub.cli.project import PROJECT
from evo_agents.hub.cli.registry import REGISTRY
from evo_agents.hub.cli.token import TOKEN, Account
from evo_agents.hub.client import (
    TOKEN_VARIABLE,
    URL_VARIABLE,
    HubError,
    NotSignedIn,
    check_url,
    env_credentials,
    hub_dir,
    load_credentials,
    login,
    remove_credentials,
)

ENV_PAIR = f"{TOKEN_VARIABLE} and {URL_VARIABLE}"  # the token and hub that come before any login (env_credentials)
# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
WHOAMI_KEYS = ("login", "admin", "token", "grants")


def last_url() -> str:
    """The hub signed in to last; HubError asking for --url when there is none."""
    try:
        return load_credentials().url
    except HubError:
        raise HubError("which hub? pass --url, for example --url https://hub.example.org") from None


def sign_out(account: Account, force: bool) -> str:
    """Revoke the token of this machine on the hub, and say what became of it; HubError when the hub refused and
    ``force`` was not given, so the token stays."""
    try:
        account.logout()
    except HubError as exc:
        if exc.status == 401:  # revoked or expired already: nothing left to revoke
            return "the token was no longer valid and is deleted from this machine"
        if force:
            return f"the token is deleted from this machine but NOT revoked ({exc})"
        raise HubError(
            f"{exc}. The token was not revoked and stays in {hub_dir()}; try again, or pass --force to delete "
            "it anyway (it then stays valid on the hub until it expires or is revoked from another machine)"
        ) from None
    return "the token is revoked and deleted from this machine"


class Login(HubCommand):
    name = "login"
    help = "sign in to a hub with GitHub (device flow) and keep a machine token"
    arguments = (Arg("--url", help="the hub, such as https://hub.example.org (default: the last one used)"),)

    def run(self, ctx: HubContext) -> Result:
        url = check_url(ctx.args.url or last_url())
        signed = login(url)
        role = " (admin)" if signed.get("admin") else ""
        lines = [f"Signed in to {url} as {signed['login']}{role}; the token is in {hub_dir() / 'token'}."]
        if env_credentials() is not None:
            lines.append(f"note: {ENV_PAIR} are set, so commands use them, not this login, until they are unset.")
        return Result(None, Text(*lines))


class Logout(HubCommand):
    name = "logout"
    help = "revoke this machine's token on the hub and delete it here"
    arguments = (
        Arg(
            "--force",
            action="store_true",
            help="delete the local token even when the hub cannot be reached to revoke it",
        ),
    )

    def run(self, ctx: HubContext) -> Result:
        if env_credentials() is not None:
            raise HubError(
                f"signed in with {ENV_PAIR}, which logout does not touch: unset them; the login in {hub_dir()} is "
                "left as it is"
            )
        try:
            hub, credentials = ctx.session
        except NotSignedIn:
            return Result(None, Text("Not signed in; nothing to do."))
        outcome = sign_out(Account(hub), ctx.args.force)
        remove_credentials()
        return Result(None, Text(f"Signed out of {credentials.url}; {outcome}."))


class Whoami(HubCommand):
    name = "whoami"
    help = "the signed-in login, whether it is an admin, and its grants"
    output = returns_object(*WHOAMI_KEYS, "url", schema="WhoAmI", added=("url",))

    def run(self, ctx: HubContext) -> Result:
        me, url = Account(ctx.hub).whoami(), ctx.credentials.url
        token = me["token"]
        return Result(
            {**me, "url": url},
            Text(
                f"{me['login']}{' (admin)' if me['admin'] else ''} on {url}",
                f"token {token['id']}: {token['kind']} on {token['host'] or 'the web'}, expires "
                f"{when(token['expires_at'])}",
                f"grants: {grants_text(me['grants'])}",
            ),
        )


@dataclass(frozen=True)
class Commands:
    """Nodes attached one after the other under the same parser, in their order: words of ``hub`` itself, with no
    group of their own."""

    nodes: tuple[Node, ...]

    def attach(self, subparsers: argparse._SubParsersAction) -> None:
        for node in self.nodes:
            node.attach(subparsers)


LOGIN, LOGOUT, WHOAMI = Login(), Logout(), Whoami()
# The node ``evo_agents.hub.cli`` attaches: hub login, logout, whoami, token, admin, project and registry, in that order.
SESSION = Commands((LOGIN, LOGOUT, WHOAMI, TOKEN, ADMIN, PROJECT, REGISTRY))
