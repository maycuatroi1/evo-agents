"""``evo-agents hub token list|revoke``: the hub tokens of the signed-in login.

``list`` prints them with kind, host, last use and expiry (``--all`` adds the revoked and expired ones), and marks the
token of this machine; ``revoke ID`` revokes one, and when it is the token of this machine deletes it here too. No
command prints a token's value. The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command
classes): :class:`Tokens` is the gateway to ``/v1/tokens``, :class:`Account` to the routes of the signed-in token
(``/v1/auth/...``), which ``hub whoami`` and ``hub logout`` ask too. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_array
from evo_agents.hub.cli.base import HubCommand, HubContext, HubRoutes, when
from evo_agents.hub.client import remove_credentials

TOKEN_KEYS = ("id", "kind", "host", "created_at", "last_used_at", "expires_at", "revoked_at", "state", "current")


class Account(HubRoutes):
    """The routes of the signed-in token: GET /v1/auth/whoami and POST /v1/auth/logout (Gateway)."""

    path = "/v1/auth"

    def whoami(self) -> dict:
        return self.call("GET", "/whoami")

    def logout(self) -> None:
        self.call("POST", "/logout")


class Tokens(HubRoutes):
    """GET /v1/tokens and DELETE /v1/tokens/{id} (Gateway)."""

    path = "/v1/tokens"

    def list(self, every: bool) -> list[dict]:
        return self.call("GET", "?all=true" if every else "")

    def revoke(self, token: int) -> None:
        self.call("DELETE", f"/{token}")


class TokenList(HubCommand):
    name = "list"
    help = "your tokens with kind, host, last use and expiry"
    arguments = (Arg("--all", action="store_true", help="include revoked and expired tokens"),)
    output = returns_array(*TOKEN_KEYS, schema="TokenRow")
    columns = (" ID", "KIND", "HOST", "CREATED", "LAST USED", "EXPIRES (UTC)", "STATE")

    def run(self, ctx: HubContext) -> Result:
        tokens = Tokens(ctx.hub).list(ctx.args.all)
        rows = Table(self.columns, [self.row(token) for token in tokens], bare=True)
        return Result(tokens, Views(rows, Text("* the token of this machine")))

    @staticmethod
    def row(token: dict) -> tuple:
        return (
            ("*" if token["current"] else " ") + str(token["id"]),
            token["kind"],
            token["host"] or "-",
            when(token["created_at"]),
            when(token["last_used_at"]),
            when(token["expires_at"]),
            token["state"],
        )


class TokenRevoke(HubCommand):
    name = "revoke"
    help = "revoke one of your tokens"
    arguments = (Arg("id", metavar="ID", type=int, help="token id, from `hub token list`"),)

    def run(self, ctx: HubContext) -> Result:
        token = ctx.args.id
        current = Account(ctx.hub).whoami()["token"]["id"]
        Tokens(ctx.hub).revoke(token)
        if token != current:
            return Result(None, Text(f"Revoked token {token}."))
        remove_credentials()
        return Result(
            None,
            Text(f"Revoked token {token}, the token of this machine; run `evo-agents hub login` to sign in again."),
        )


TOKEN = Group("token", "your hub tokens", [TokenList(), TokenRevoke()])
