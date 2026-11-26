"""``evo-agents hub email list|add|remove``: the emails of a member on the hub (``server.user_emails``), the addresses
the hub names that member by, such as the author of a commit.

Each command acts on the signed-in member's own emails; ``--user LOGIN`` acts on another member's, which a hub admin
alone may do. ``add`` and ``remove`` check the address before they ask the hub; ``remove`` finds the row of the
address in the member's list and removes it by its id, so the address never goes in a URL. ``--json`` prints the
hub's rows (``UserEmail``). Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from urllib.parse import urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.user_emails import NOREPLY_DOMAIN, email_key, email_problem

EXIT_USAGE = 2
EMAIL_KEYS = ("id", "email", "login", "created_at")


def _path(user: str | None) -> str:
    return "/v1/emails" + (f"?{urlencode({'user': user})}" if user else "")


def _checked(email: str) -> str | None:
    """``email`` without the spaces around it; None, with an error line, when the hub would refuse it."""
    email = email.strip()
    problem = email_problem(email)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return None
    return email


def _whose(user: str | None) -> str:
    return user or "you"


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(args.user))
    if args.json:
        print_json(found)
        return 0
    if found:
        table(("ID", "EMAIL", "ADDED (UTC)"), [(e["id"], e["email"], when(e["created_at"])) for e in found])
    else:
        print(f"No email listed for {_whose(args.user)}.")
    print(f"GitHub's noreply addresses (@{NOREPLY_DOMAIN}) name their login and need no row.")
    return 0


@client_command
def cmd_add(args) -> int:
    email = _checked(args.email)
    if email is None:
        return EXIT_USAGE
    hub, _ = signed_in()
    added = hub.call("POST", _path(args.user), {"email": email})
    if args.json:
        print_json(added)
        return 0
    print(f"{added['email']} is an email of {added['login']} (id {added['id']}).")
    return 0


@client_command
def cmd_remove(args) -> int:
    email = _checked(args.email)
    if email is None:
        return EXIT_USAGE
    hub, _ = signed_in()
    listed = hub.call("GET", _path(args.user))
    row = next((e for e in listed if email_key(e["email"]) == email_key(email)), None)
    if row is None:
        print(
            f"error: {email} is not an email of {_whose(args.user)}: see `evo-agents hub email list`", file=sys.stderr
        )
        return 1
    removed = hub.call("DELETE", f"/v1/emails/{row['id']}")
    if args.json:
        print_json(removed)
        return 0
    print(f"Removed {removed['email']} from the emails of {removed['login']}.")
    return 0


def _user_option(parser) -> None:
    parser.add_argument("--user", metavar="LOGIN", help="another member's emails (a hub admin only)")


def register_emails(hsub) -> None:
    email = hsub.add_parser("email", help="your emails on the hub, which name you as a commit's author")
    esub = email.add_subparsers(dest="email_command", required=True)

    listed = esub.add_parser("list", help="the emails listed for you, or for --user")
    _user_option(listed)
    json_option(listed, returns_array(*EMAIL_KEYS, schema="UserEmail"))
    listed.set_defaults(func=cmd_list)

    add = esub.add_parser("add", help="list an email for you, or for --user; one member an address")
    add.add_argument("email", metavar="EMAIL", help="the address; case does not matter")
    _user_option(add)
    json_option(add, returns_object(*EMAIL_KEYS, schema="UserEmail"))
    add.set_defaults(func=cmd_add)

    remove = esub.add_parser("remove", help="drop one of your emails, or one of --user's")
    remove.add_argument("email", metavar="EMAIL", help="the address, as `hub email list` shows it, any case")
    _user_option(remove)
    json_option(remove, returns_object(*EMAIL_KEYS, schema="UserEmail"))
    remove.set_defaults(func=cmd_remove)
