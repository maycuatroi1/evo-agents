"""``evo-agents hub account policy PROJECT``: the member's policy for their accounts in a project
(``evo_agents.hub.server.accounts``, ``docs/credentials.md``, section "Accounts"). Without a flag it prints the policy;
a flag sets it over the policy as it is now, ``--reset`` drops it so the defaults hold again.

``--strategy`` names how a run picks among the accounts (``evo_agents.hub.accounts.STRATEGIES``); with
``quota-pacing``, ``--hard-reserve-pct`` is the share of a weekly window kept back while another account has more and
``--five-hour-ceiling-pct`` the five-hour use above which an account waits while a cooler one is ready. Standard
library only.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import quote

from evo_agents.hub.accounts import (
    MAX_COOLDOWN_SECONDS,
    MAX_FAILOVERS,
    MAX_PCT,
    MIN_COOLDOWN_SECONDS,
    QUOTA_PACING,
    STRATEGIES,
)
from evo_agents.hub.cli_client import client_command, print_json, signed_in, when
from evo_agents.hub.contract import json_option, returns_object

EXIT_USAGE = 2
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


def _usage_error(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def out_of_bounds(given: dict, bounds: dict = BOUNDS) -> str | None:
    """Why a number of ``given`` is not one the hub takes; None when each is."""
    for key, value in given.items():
        if key in bounds and not bounds[key][0] <= value <= bounds[key][1]:
            low, high, flag = bounds[key]
            return f"{flag} is a whole number from {low} to {high}, not {value}"
    return None


def _policy_path(project: str) -> str:
    return f"/v1/account-policies/{quote(project, safe='')}"


def _print_policy(policy: dict) -> None:
    failover = f"on, at most {policy['max_failovers']} time(s)" if policy["failover"] else "off"
    origin = f"set {when(policy['updated_at'])} UTC" if policy["updated_at"] else "the defaults"
    print(
        f"Project {policy['project']} ({origin}): strategy {policy['strategy']}, cooldown "
        f"{policy['cooldown_seconds']} s, failover {failover}."
    )
    if policy["strategy"] == QUOTA_PACING:
        print(
            f"  Quota pacing: reserve {policy.get('hard_reserve_pct')}% of a weekly window, five-hour ceiling "
            f"{policy.get('five_hour_ceiling_pct')}%."
        )


@client_command
def cmd_account_policy(args) -> int:
    given = {key: getattr(args, key) for key in POLICY_FLAGS if getattr(args, key) is not None}
    if args.reset and given:
        return _usage_error("--reset drops the policy: give it alone")
    if (refusal := out_of_bounds(given)) is not None:
        return _usage_error(refusal)
    hub, _ = signed_in()
    path = _policy_path(args.project)
    if args.reset:
        hub.call("DELETE", path)
        policy = hub.call("GET", path)
    elif given:
        now = hub.call("GET", path) or {}
        policy = hub.call("PUT", path, {**{key: now[key] for key in POLICY_FLAGS if key in now}, **given})
    else:
        policy = hub.call("GET", path)
    if args.json:
        print_json(policy)
    else:
        _print_policy(policy or {})
    return 0


def register_policy(asub) -> None:
    """``account policy PROJECT``, added to the subparsers ``asub`` of ``hub account``."""
    policy = asub.add_parser(
        "policy", help="show your policy for a project; with a flag, set it over the policy as it is now"
    )
    policy.add_argument("project", metavar="PROJECT")
    policy.add_argument(
        "--strategy",
        choices=STRATEGIES,
        help="how a run picks among your accounts; quota-pacing spends the weekly quota that resets soonest first",
    )
    policy.add_argument(
        "--cooldown",
        dest="cooldown_seconds",
        type=int,
        metavar="SECONDS",
        help="how long an account cools down after a usage limit that gave no reset time",
    )
    policy.add_argument(
        "--failover",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="whether a run that hits a usage limit goes on with another account",
    )
    policy.add_argument(
        "--max-failovers",
        type=int,
        metavar="N",
        help=f"0 to {MAX_FAILOVERS}: at most how many times a run moves on",
    )
    policy.add_argument(
        "--hard-reserve-pct",
        type=int,
        metavar="PCT",
        help=f"0 to {MAX_PCT}, quota-pacing: the share of a weekly window kept back while another account has more",
    )
    policy.add_argument(
        "--five-hour-ceiling-pct",
        type=int,
        metavar="PCT",
        help=f"1 to {MAX_PCT}, quota-pacing: the five-hour use above which an account waits while a cooler one is ready",
    )
    policy.add_argument("--reset", action="store_true", help="drop your policy: the defaults hold again")
    json_option(policy, returns_object(*POLICY_KEYS, schema="AccountPolicy"))
    policy.set_defaults(func=cmd_account_policy)
