"""``evo-agents hub account policy PROJECT``: the member's policy for their accounts in a project
(``evo_agents.hub.server.accounts.service.accounts``, ``docs/credentials.md``, section "Accounts"). Without a flag it prints the policy;
a flag sets it over the policy as it is now, ``--reset`` drops it so the defaults hold again.

``--strategy`` names how a run picks among the accounts (``evo_agents.hub.accounts.STRATEGIES``); with
``quota-pacing``, ``--hard-reserve-pct`` is the share of a weekly window kept back while another account has more and
``--five-hour-ceiling-pct`` the five-hour use above which an account waits while a cooler one is ready. Standard
library only.

The command is :class:`AccountPolicy`, a class of ``evo_agents.clikit``, over the gateway
``cli.secret.resources.AccountPolicies``; :func:`out_of_bounds` and :func:`policy_lines` are its rules and its view.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Result, Text, UsageError, returns_object
from evo_agents.hub.accounts import (
    MAX_COOLDOWN_SECONDS,
    MAX_FAILOVERS,
    MAX_PCT,
    MIN_COOLDOWN_SECONDS,
    QUOTA_PACING,
    STRATEGIES,
)
from evo_agents.hub.cli.base import HubCommand, HubContext, when
from evo_agents.hub.cli.secret.resources import AccountPolicies

# The keys of a policy in `hub account policy --json`.
POLICY_KEYS = (
    "strategy",
    "cooldown_seconds",
    "failover",
    "max_failovers",
    "project",
    "updated_at",
    "hard_reserve_pct",
    "five_hour_ceiling_pct",
)
POLICY_FLAGS = (
    "strategy",
    "cooldown_seconds",
    "failover",
    "max_failovers",
    "hard_reserve_pct",
    "five_hour_ceiling_pct",
)
BOUNDS = {
    "cooldown_seconds": (MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS, "--cooldown"),
    "max_failovers": (0, MAX_FAILOVERS, "--max-failovers"),
    "hard_reserve_pct": (0, MAX_PCT, "--hard-reserve-pct"),
    "five_hour_ceiling_pct": (1, MAX_PCT, "--five-hour-ceiling-pct"),
}


def out_of_bounds(given: dict, bounds: dict = BOUNDS) -> str | None:
    """Why a number of ``given`` is not one the hub takes; None when each is."""
    for key, value in given.items():
        if key in bounds and not bounds[key][0] <= value <= bounds[key][1]:
            low, high, flag = bounds[key]
            return f"{flag} is a whole number from {low} to {high}, not {value}"
    return None


def policy_lines(policy: dict) -> list[str]:
    """What ``account policy`` prints of a policy."""
    failover = f"on, at most {policy['max_failovers']} time(s)" if policy["failover"] else "off"
    origin = f"set {when(policy['updated_at'])} UTC" if policy["updated_at"] else "the defaults"
    lines = [
        f"Project {policy['project']} ({origin}): strategy {policy['strategy']}, cooldown "
        f"{policy['cooldown_seconds']} s, failover {failover}."
    ]
    if policy["strategy"] == QUOTA_PACING:
        lines.append(
            f"  Quota pacing: reserve {policy.get('hard_reserve_pct')}% of a weekly window, five-hour ceiling "
            f"{policy.get('five_hour_ceiling_pct')}%."
        )
    return lines


def given_flags(args: argparse.Namespace) -> dict:
    """The fields of the policy the flags of ``args`` set."""
    return {key: getattr(args, key) for key in POLICY_FLAGS if getattr(args, key) is not None}


class AccountPolicy(HubCommand):
    name = "policy"
    help = "show your policy for a project; with a flag, set it over the policy as it is now"
    arguments = (
        Arg("project", metavar="PROJECT"),
        Arg(
            "--strategy",
            choices=STRATEGIES,
            help="how a run picks among your accounts; quota-pacing spends the weekly quota that resets soonest first",
        ),
        Arg(
            "--cooldown",
            dest="cooldown_seconds",
            type=int,
            metavar="SECONDS",
            help="how long an account cools down after a usage limit that gave no reset time",
        ),
        Arg(
            "--failover",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="whether a run that hits a usage limit goes on with another account",
        ),
        Arg(
            "--max-failovers",
            type=int,
            metavar="N",
            help=f"0 to {MAX_FAILOVERS}: at most how many times a run moves on",
        ),
        Arg(
            "--hard-reserve-pct",
            type=int,
            metavar="PCT",
            help=f"0 to {MAX_PCT}, quota-pacing: the share of a weekly window kept back while another account has more",
        ),
        Arg(
            "--five-hour-ceiling-pct",
            type=int,
            metavar="PCT",
            help=f"1 to {MAX_PCT}, quota-pacing: the five-hour use above which an account waits while a cooler one is "
            "ready",
        ),
        Arg("--reset", action="store_true", help="drop your policy: the defaults hold again"),
    )
    output = returns_object(*POLICY_KEYS, schema="AccountPolicy")

    def check(self, args: argparse.Namespace) -> None:
        given = given_flags(args)
        if args.reset and given:
            raise UsageError("--reset drops the policy: give it alone")
        if (refusal := out_of_bounds(given)) is not None:
            raise UsageError(refusal)

    def run(self, ctx: HubContext) -> Result:
        policies, project, given = AccountPolicies.of(ctx), ctx.args.project, given_flags(ctx.args)
        if ctx.args.reset:
            policies.delete(project)
            policy = policies.get(project)
        elif given:
            now = policies.get(project) or {}
            policy = policies.put(project, {**{key: now[key] for key in POLICY_FLAGS if key in now}, **given})
        else:
            policy = policies.get(project)
        if ctx.args.json:
            return Result(policy)
        return Result(policy, Text(*policy_lines(policy or {})))


ACCOUNT_POLICY = AccountPolicy()
