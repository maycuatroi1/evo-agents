"""``evo-agents hub crm list|show|delete``: the contacts of a project that enabled the core plugin crm, through its routes
under ``/v1/x/crm/<project>`` (``docs/crm-plugin.md``). The plugin's code is ``evo_agents.core_plugins.crm``, which this
client never imports: it speaks HTTP only.

``list --project X`` prints the contacts and how many are in each stage (``--stage``, ``--source`` narrow it), ``show
--project X LOGIN`` one contact with its touchpoints and the stages it reached, ``delete --project X LOGIN`` deletes a
contact on its request with every touchpoint (an admin of the project does; ``--yes`` answers the question). ``--json``
prints what the hub answered. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object

LIST_KEYS = ("project", "counts", "contacts")
SHOW_KEYS = ("login", "profile_url", "source", "stage", "first_seen_at", "stage_at", "touchpoints", "history")
DELETE_KEYS = ("deleted", "touchpoints")
STAGES = ("lead", "invited", "activated", "retained")
SOURCES = ("invite_request", "invite_link", "grant", "issue", "pull_request", "discussion")


def _path(project: str, login: str | None = None, **query: str | None) -> str:
    path = f"/v1/x/crm/{quote(project, safe='')}/contacts"
    if login:
        path += f"/{quote(login, safe='')}"
    asked = {name: value for name, value in query.items() if value}
    return f"{path}?{urlencode(asked)}" if asked else path


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(args.project, stage=args.stage, source=args.source))
    if args.json:
        print_json(found)
        return 0
    print("  ".join(f"{stage} {found['counts'].get(stage, 0)}" for stage in STAGES))
    rows = [(c["login"], c["stage"], c["source"], c["first_seen_at"][:10]) for c in found["contacts"]]
    if rows:
        table(("login", "stage", "source", "first seen"), rows)
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(args.project, args.login))
    if args.json:
        print_json(found)
        return 0
    print(f"{found['login']}  {found['profile_url']}")
    print(f"source {found['source']}, stage {found['stage']} since {found['stage_at'][:10]}")
    print("Stages: " + ", ".join(f"{h['stage']} {h['reached_at'][:10]}" for h in found["history"]))
    rows = [
        (t["happened_at"][:10], t["kind"], t["ref"], t.get("url") or t.get("note") or "") for t in found["touchpoints"]
    ]
    if rows:
        table(("when", "kind", "ref", "link or note"), rows)
    return 0


@client_command
def cmd_delete(args) -> int:
    if not args.yes:
        print(
            f"Delete {args.login} from project {args.project} with every touchpoint? This cannot be undone. "
            "Run again with --yes.",
            file=sys.stderr,
        )
        return 2
    hub, _ = signed_in()
    found = hub.call("DELETE", _path(args.project, args.login))
    if args.json:
        print_json(found)
    else:
        print(f"Deleted {args.login} and {found['touchpoints']} touchpoints")
    return 0


def register_crm(hsub) -> None:
    crm = hsub.add_parser("crm", help="the contacts of a project (plugin crm)")
    csub = crm.add_subparsers(dest="crm_command", required=True)
    listing = csub.add_parser("list", help="the contacts and how many are in each stage")
    listing.add_argument("--project", required=True, metavar="PROJECT", help="the project that enabled plugin crm")
    listing.add_argument("--stage", choices=STAGES, help="only contacts in this stage")
    listing.add_argument("--source", choices=SOURCES, help="only contacts first seen from this source")
    json_option(listing, returns_object(*LIST_KEYS, schema="CrmContacts"))
    listing.set_defaults(func=cmd_list)
    show = csub.add_parser("show", help="one contact with its touchpoints and the stages it reached")
    show.add_argument("login", metavar="LOGIN", help="the contact's GitHub login")
    show.add_argument("--project", required=True, metavar="PROJECT", help="the project that enabled plugin crm")
    json_option(show, returns_object(*SHOW_KEYS, schema="CrmContactDetail"))
    show.set_defaults(func=cmd_show)
    remove = csub.add_parser("delete", help="delete a contact on its request, with every touchpoint (admin)")
    remove.add_argument("login", metavar="LOGIN", help="the contact's GitHub login")
    remove.add_argument("--project", required=True, metavar="PROJECT", help="the project that enabled plugin crm")
    remove.add_argument("--yes", action="store_true", help="delete without asking again")
    json_option(remove, returns_object(*DELETE_KEYS, schema="CrmDeleted"))
    remove.set_defaults(func=cmd_delete)
