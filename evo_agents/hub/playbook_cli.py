"""``evo-agents hub curator playbook`` and ``evo-agents hub curator earlier``: what the Curator's review runs of a
project remember of it.

``curator playbook`` lists the entries of the project's playbook (GET /v1/projects/{p}/curator/playbook,
``evo_agents.hub.playbook``), the most helpful first, by ``--lens``, the retired ones too with ``--retired``; only the
agent of a review run changes it, by deltas (``evo-agents worker playbook``). ``curator earlier`` lists what earlier
review nights found, newest first, with what became of each finding (open, proposed, rejected, merged, kept or
reverted) and its proposals, and the proposals the owner rejected lately (GET .../curator/earlier).

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.plan_cli import project_of_args
from evo_agents.hub.review import LENSES

PLAYBOOK_KEYS = ("project", "entries", "live", "retired")
EARLIER_KEYS = ("project", "findings", "rejected")
STATUSES = ("open", "proposed", "rejected", "merged", "kept", "reverted")  # server.earlier.STATUSES


def _path(project: str, name: str, **params) -> str:
    asked = {key: value for key, value in params.items() if value is not None}
    query = f"?{urlencode(asked)}" if asked else ""
    return f"/v1/projects/{quote(project, safe='')}/curator/{name}{query}"


@client_command
def cmd_playbook(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("GET", _path(project, "playbook", lens=args.lens, retired="true" if args.retired else None))
    if args.json:
        print_json(found)
        return 0
    rows = [
        (
            entry["ref"],
            entry["lens"] or "every lens",
            str(entry["helpful"]),
            str(entry["harmful"]),
            "retired" if entry["retired"] else "live",
            entry["text"],
        )
        for entry in found["entries"]
    ]
    if rows:
        table(("ENTRY", "LENS", "HELPFUL", "HARMFUL", "STATE", "TEXT"), rows)
    print(f"The playbook of {project}: {found['live']} live entries, {found['retired']} retired")
    return 0


@client_command
def cmd_earlier(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("GET", _path(project, "earlier", lens=args.lens, status=args.status, limit=args.limit))
    if args.json:
        print_json(found)
        return 0
    rows = [
        (
            f"#{item['id']}",
            item["lens"],
            item["night"] or "-",
            item["status"] + (" (closed)" if item["closed"] else ""),
            ", ".join(f"#{p['id']} {p['status']}" for p in item["proposals"]) or "-",
            item["title"],
        )
        for item in found["findings"]
    ]
    if rows:
        table(("FINDING", "LENS", "NIGHT", "STATUS", "PROPOSALS", "TITLE"), rows)
    print(f"{len(rows)} finding(s) of earlier nights in {project}")
    for item in found["rejected"]:
        note = f" ({item['note']})" if item["note"] else ""
        print(f"rejected lately: #{item['id']} {item['lens']} {item['kind']}: {item['title']}{note}")
    return 0


def register_playbook(csub, with_project) -> None:
    """``curator playbook`` and ``curator earlier``."""
    playbook = csub.add_parser("playbook", help="the Curator's playbook of the project, the most helpful entries first")
    with_project(playbook)
    playbook.add_argument("--lens", choices=list(LENSES), help="its entries and those of every lens")
    playbook.add_argument("--retired", action="store_true", help="the retired entries too")
    json_option(playbook, returns_object(*PLAYBOOK_KEYS, schema="PlaybookView"))
    playbook.set_defaults(func=cmd_playbook)
    earlier = csub.add_parser("earlier", help="what earlier review nights found and what became of it, newest first")
    with_project(earlier)
    earlier.add_argument("--lens", choices=list(LENSES))
    earlier.add_argument("--status", choices=STATUSES)
    earlier.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="N", help="1 to 100")
    json_option(earlier, returns_object(*EARLIER_KEYS, schema="EarlierView"))
    earlier.set_defaults(func=cmd_earlier)
