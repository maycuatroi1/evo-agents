"""``evo-agents hub secret share|unshare`` and ``hub account share|unshare``: the owner shares a secret or an account
with a member, a unit of the org chart or everyone, for some of the projects it is bound to, and revokes a share
(``docs/credentials.md``; the rules are ``evo_agents.hub.credential_shares``).

``share NAME`` names the grantee with exactly one of ``--login``, ``--unit`` and ``--all``, and the projects with one
``--project`` or more (POST /v1/secrets/{name}/shares; an account is an env secret, so both nouns take the same route).
``--level``, ``--budget-usd``, ``--usage-ceiling``, ``--max-running`` and ``--expires YYYY-MM-DD`` (00:00 UTC of that
day, as for a secret) bound its use. A personal Claude subscription token is shared only with ``--ack-terms``: without
it the hub refuses with TERMS_WARNING, which the command prints whole on its own before exiting non-zero.
``unshare --share ID`` revokes a share (DELETE /v1/shares/{id}). ``hub secret list`` and ``hub account show`` list
the live shares of each. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from urllib.parse import quote

from evo_agents.hub.cli_client import EXIT_FAILED, client_command, print_json, signed_in
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.credential_shares import MAX_BUDGET_USD, MAX_RUNNING, TERMS_WARNING
from evo_agents.hub.org import LEVELS

EXIT_USAGE = 2
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The keys of a share in `hub secret share --json` and `hub account unshare --json` (`evo-agents hub contract print`).
SHARE_KEYS = (
    "id",
    "secret",
    "credential",
    "grantee_kind",
    "grantee",
    "projects",
    "level",
    "budget_usd",
    "usage_ceiling_pct",
    "max_running",
    "expires_at",
    "terms_ack_at",
    "created_by",
    "created_at",
    "revoked_at",
    "revoked_by",
)


def _usage_error(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def share_expires_at(day: str) -> str | None:
    """``YYYY-MM-DD`` as the moment the share ends, 00:00 UTC of that day in ISO 8601; None when it is no day."""
    try:
        moment = datetime.strptime(day, "%Y-%m-%d") if DAY.fullmatch(day) else None
    except ValueError:
        return None
    return None if moment is None else moment.replace(tzinfo=UTC).isoformat()


def share_body(args) -> dict | str:
    """The body of POST /v1/secrets/{name}/shares from ``args``; a sentence saying what is wrong instead."""
    body: dict = {"projects": list(dict.fromkeys(args.project)), "terms_ack": bool(args.ack_terms)}
    if args.login is not None:
        body.update(grantee_kind="user", login=args.login)
    elif args.unit is not None:
        body.update(grantee_kind="unit", unit=args.unit)
    else:
        body["grantee_kind"] = "all"
    if args.budget_usd is not None and not 0 < args.budget_usd <= MAX_BUDGET_USD:
        return f"--budget-usd takes more than 0 and at most {MAX_BUDGET_USD}"
    if args.usage_ceiling is not None and not 1 <= args.usage_ceiling <= 100:
        return "--usage-ceiling takes a percent from 1 to 100"
    if args.max_running is not None and not 1 <= args.max_running <= MAX_RUNNING:
        return f"--max-running takes 1 to {MAX_RUNNING}"
    limits = {
        "level": args.level,
        "budget_usd": args.budget_usd,
        "usage_ceiling_pct": args.usage_ceiling,
        "max_running": args.max_running,
    }
    body.update({key: value for key, value in limits.items() if value is not None})
    if args.expires is not None:
        ends = share_expires_at(args.expires)
        if ends is None:
            return f"--expires takes a day as YYYY-MM-DD, such as 2027-01-31, not {args.expires!r}"
        body["expires_at"] = ends
    return body


def _grantee_text(share: dict) -> str:
    if share["grantee_kind"] == "all":
        return "everyone on the hub"
    noun = "member" if share["grantee_kind"] == "user" else "unit"
    return f"{noun} {share['grantee']}"


@client_command
def cmd_share(args) -> int:
    body = share_body(args)
    if isinstance(body, str):
        return _usage_error(body)
    hub, _ = signed_in()
    try:
        share = hub.call("POST", f"/v1/secrets/{quote(args.name, safe='')}/shares", body)
    except HubError as exc:
        if exc.status == 409 and TERMS_WARNING in str(exc):
            print(TERMS_WARNING, file=sys.stderr)
            print(f"error: {args.name} was not shared: give --ack-terms to share it anyway", file=sys.stderr)
            return EXIT_FAILED
        raise
    if args.json:
        print_json(share)
        return 0
    print(
        f"Shared {args.noun} {share['secret']} with {_grantee_text(share)} for {', '.join(share['projects'])}: "
        f"share {share['id']}. `evo-agents hub {args.noun} unshare --share {share['id']}` revokes it."
    )
    return 0


@client_command
def cmd_unshare(args) -> int:
    if args.share < 1:
        return _usage_error(f"--share takes the id of a share, a whole number from 1, not {args.share}")
    hub, _ = signed_in()
    share = hub.call("DELETE", f"/v1/shares/{args.share}")
    if args.json:
        print_json(share)
        return 0
    print(f"Revoked share {share['id']} of {share['secret']}: no new run of its grantee gets it.")
    return 0


def register_share_commands(sub, noun: str) -> None:
    """``share`` and ``unshare`` under ``hub secret`` or ``hub account`` (``noun``)."""
    share = sub.add_parser(
        "share", help=f"share one of your {noun}s with a member, a unit or everyone, for some of its projects"
    )
    share.add_argument("name", metavar="NAME", help=f"one of your {noun}s")
    grantee = share.add_mutually_exclusive_group(required=True)
    grantee.add_argument("--login", metavar="LOGIN", help="share it with this member")
    grantee.add_argument("--unit", metavar="SLUG", help="share it with this unit of the org chart and every unit below")
    grantee.add_argument("--all", action="store_true", help="share it with everyone on the hub")
    share.add_argument(
        "--project",
        required=True,
        action="append",
        metavar="PROJECT",
        help=f"a project the {noun} is bound to whose runs may use it; repeat it for several",
    )
    share.add_argument("--level", choices=LEVELS, help="the autonomy level a run using it is held to")
    share.add_argument("--budget-usd", type=float, metavar="N", help="the most its runs may cost a month, in USD")
    share.add_argument(
        "--usage-ceiling", type=int, metavar="PCT", help="1 to 100: no run takes it while its usage is at this percent"
    )
    share.add_argument("--max-running", type=int, metavar="N", help="the most runs that may hold it at once")
    share.add_argument("--expires", metavar="YYYY-MM-DD", help="the share ends at 00:00 UTC of this day")
    share.add_argument(
        "--ack-terms",
        action="store_true",
        help="acknowledge the terms warning, which sharing a personal Claude subscription token needs",
    )
    json_option(share, returns_object(*SHARE_KEYS, schema="CredentialShare"))
    share.set_defaults(func=cmd_share, noun=noun)

    unshare = sub.add_parser("unshare", help=f"revoke a share of one of your {noun}s (an admin may revoke any)")
    unshare.add_argument("--share", required=True, type=int, metavar="ID", help="the id of the share")
    json_option(unshare, returns_object(*SHARE_KEYS, schema="CredentialShare"))
    unshare.set_defaults(func=cmd_unshare, noun=noun)
