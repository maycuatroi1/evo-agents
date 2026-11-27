"""``evo-agents hub account``: the caller's env secrets as accounts, one of which a run takes among several for the same
variable, and their policy per project (``docs/credentials.md``, section "Accounts").

``account list`` prints them and the provider accounts, never a value (GET /v1/accounts); ``account show`` prints one
(GET /v1/accounts/{name}), and with ``--owner LOGIN`` one that member shares with the caller. ``account set`` changes
the weight, priority or weekly budget of one, ``account pause`` and ``account resume`` keep new runs off it or let
them back (PATCH /v1/accounts/{name}); a resume also ends a cooldown, save one that needs a new sign-in. ``account list``
and ``account show`` remind the member to renew an account that ends within RENEW_WITHIN_DAYS days, or has ended
(``cli.account.views``).

``account login`` and ``account oauth-login`` add a Claude account or renew its token (``cli.account.login``),
``account share`` and ``unshare`` share one (``cli.secret.shares``), and ``account policy PROJECT`` prints or sets the
caller's policy for a project (``cli.account.policy``).

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes) over the gateway
``cli.secret.resources.Accounts``; ``ACCOUNT`` is their group. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from evo_agents.clikit import (
    Arg,
    Group,
    Result,
    Table,
    Text,
    UsageError,
    Views,
    json_option,
    returns_array,
    returns_object,
)
from evo_agents.hub.accounts import MAX_PRIORITY, MAX_WEIGHT
from evo_agents.hub.cli.account.login import ACCOUNT_LOGIN, ACCOUNT_OAUTH_LOGIN
from evo_agents.hub.cli.account.policy import ACCOUNT_POLICY
from evo_agents.hub.cli.account.views import (
    ACCOUNT_COLUMNS,
    ACCOUNT_KEYS,
    account_line,
    account_lines,
    account_row,
    reminders,
)
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.secret.bindings import check_secret_name
from evo_agents.hub.cli.secret.resources import Accounts
from evo_agents.hub.cli.secret.shares import ACCOUNT_SHARE, ACCOUNT_UNSHARE

MAX_BUDGET_USD = 1_000_000  # evo_agents.hub.server.accounts.models.accounts.MAX_BUDGET_USD
BOUNDS = {
    "weight": (0, MAX_WEIGHT, "--weight"),
    "priority": (0, MAX_PRIORITY, "--priority"),
}
ACCOUNT_OUTPUT = returns_object(*ACCOUNT_KEYS, schema="Account")
NAME = Arg("name", metavar="NAME", help="one of your env secrets")


def out_of_bounds(given: dict) -> str | None:
    """Why a number of ``given`` is not one the hub takes; None when each is."""
    for key, value in given.items():
        if key in BOUNDS and not BOUNDS[key][0] <= value <= BOUNDS[key][1]:
            low, high, flag = BOUNDS[key]
            return f"{flag} is a whole number from {low} to {high}, not {value}"
    return None


def budget_body(args: argparse.Namespace) -> dict:
    """The weekly budget fields ``account set`` was given: a number, or null for --no-weekly-budget; ValueError for a
    reset time that is no ISO 8601 instant."""
    body: dict = {}
    if args.no_weekly_budget:
        body["weekly_budget_usd"] = None
    elif args.weekly_budget_usd is not None:
        body["weekly_budget_usd"] = args.weekly_budget_usd
    if args.weekly_reset_at is not None:
        moment = datetime.fromisoformat(args.weekly_reset_at.replace("Z", "+00:00"))
        moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        body["weekly_reset_at"] = moment.astimezone(UTC).isoformat()
    return body


def account_change(args: argparse.Namespace) -> dict:
    """The body of PATCH /v1/accounts/{name} for ``account set``; UsageError for options that cannot make one."""
    body = {key: getattr(args, key) for key in ("weight", "priority") if getattr(args, key) is not None}
    if args.no_weekly_budget and args.weekly_budget_usd is not None:
        raise UsageError("give --weekly-budget-usd or --no-weekly-budget, not both")
    if args.weekly_budget_usd is not None and not 0 < args.weekly_budget_usd <= MAX_BUDGET_USD:
        raise UsageError(f"--weekly-budget-usd is above 0 and at most {MAX_BUDGET_USD}")
    try:
        body |= budget_body(args)
    except ValueError:
        raise UsageError("--weekly-reset-at is an ISO 8601 instant, such as 2026-11-16T00:00:00Z") from None
    if not body:
        raise UsageError("give --weight, --priority, --weekly-budget-usd, --no-weekly-budget or --weekly-reset-at")
    if (refusal := out_of_bounds(body)) is not None:
        raise UsageError(refusal)
    return body


class AccountList(HubCommand):
    name = "list"
    help = "your accounts: variable, state, weight, priority, usage, runs and cost, never the value"
    output = returns_array(*ACCOUNT_KEYS, schema="Account")

    def run(self, ctx: HubContext) -> Result:
        found = Accounts.of(ctx).list()
        if ctx.args.json:
            return Result(found)
        if not found:
            return Result(
                found, Text("You keep no env secret on this hub; `evo-agents hub secret set --kind env` adds one.")
            )
        due = reminders(found, datetime.now(UTC))
        table = Table(ACCOUNT_COLUMNS, [account_row(a) for a in found])
        return Result(found, Views(table, Text("", *due)) if due else table)


def configure_named(command: HubCommand, parser: argparse.ArgumentParser) -> None:
    """NAME, ``--json``, then the command's own arguments: the order ``account show`` and ``set`` always had."""
    NAME.add_to(parser)
    json_option(parser, ACCOUNT_OUTPUT)
    for arg in command.arguments:
        arg.add_to(parser)
    parser.set_defaults(func=command)


class AccountShow(HubCommand):
    name = "show"
    help = "one account: state, last usage, runs and cost over 24 hours and 7 days and by principal, runs holding it"
    arguments = (Arg("--owner", metavar="LOGIN", help="an account LOGIN shared with you: your runs' figures alone"),)
    output = ACCOUNT_OUTPUT

    def configure(self, parser: argparse.ArgumentParser) -> None:
        configure_named(self, parser)

    def check(self, args: argparse.Namespace) -> None:
        check_secret_name(args.name)

    def run(self, ctx: HubContext) -> Result:
        account = Accounts.of(ctx).get(ctx.args.name, ctx.args.owner)
        if ctx.args.json:
            return Result(account)
        return Result(account, Text(*account_lines(account)))


class AccountSet(HubCommand):
    name = "set"
    help = "change the weight or priority of an account"
    arguments = (
        Arg(
            "--weight",
            type=int,
            metavar="N",
            help=f"0 to {MAX_WEIGHT}: its share in a weighted-round-robin pool; 0 leaves the pool",
        ),
        Arg(
            "--priority",
            type=int,
            metavar="N",
            help=f"0 to {MAX_PRIORITY}: the lower, the sooner a fill-first pool takes it",
        ),
        Arg(
            "--weekly-budget-usd",
            type=float,
            metavar="USD",
            help="what a week of the account holds, for a provider with no quota API: its runs' cost over it is its "
            "seven_day window",
        ),
        Arg("--no-weekly-budget", action="store_true", help="drop the account's weekly budget"),
        Arg(
            "--weekly-reset-at",
            metavar="WHEN",
            help="an ISO 8601 instant (UTC when no zone) its budget weeks start from, every 7 days; default Monday "
            "00:00 UTC",
        ),
    )
    output = ACCOUNT_OUTPUT

    def configure(self, parser: argparse.ArgumentParser) -> None:
        configure_named(self, parser)

    def check(self, args: argparse.Namespace) -> None:
        account_change(args)
        check_secret_name(args.name)

    def run(self, ctx: HubContext) -> Result:
        account = Accounts.of(ctx).change(ctx.args.name, account_change(ctx.args))
        return Result(account, Text(account_line(account)))


class AccountPause(HubCommand):
    """``account pause`` and ``account resume``, one instance each: the same PATCH with ``paused`` set or not."""

    arguments = (NAME,)
    output = ACCOUNT_OUTPUT

    def __init__(self, name: str, help: str, paused: bool):
        self.word, self.text, self.paused = name, help, paused

    def attach(self, subparsers: argparse._SubParsersAction) -> None:
        self.configure(subparsers.add_parser(self.word, help=self.text))

    def check(self, args: argparse.Namespace) -> None:
        check_secret_name(args.name)

    def run(self, ctx: HubContext) -> Result:
        account = Accounts.of(ctx).change(ctx.args.name, {"paused": self.paused})
        return Result(account, Text(account_line(account)))


ACCOUNT_PAUSE = AccountPause("pause", "keep new runs off an account; a run that holds it keeps it", True)
ACCOUNT_RESUME = AccountPause(
    "resume",
    "let new runs take the account again: ends a pause, and a cooldown after a usage limit that reset early",
    False,
)

ACCOUNT = Group(
    "account",
    "your env secrets as accounts runs rotate among, and your policy for each project",
    [
        AccountList(),
        AccountShow(),
        AccountSet(),
        ACCOUNT_PAUSE,
        ACCOUNT_RESUME,
        ACCOUNT_LOGIN,
        ACCOUNT_OAUTH_LOGIN,
        ACCOUNT_SHARE,
        ACCOUNT_UNSHARE,
        ACCOUNT_POLICY,
    ],
)
