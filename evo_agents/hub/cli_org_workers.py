"""``evo-agents hub org worker list|share|unshare``: whom a shared worker is shared with, over /v1/org/workers
(``evo_agents.hub.server.worker_shares``; plan credential-sharing, criterion a5).

``list`` prints the caller's live workers (every live one for a hub admin): owner, name, whether it is shared, the
units it is narrowed to and what the newest report of its doctor found. ``share WORKER --unit SLUG`` narrows a worker
to a unit of the org chart, sharing it when it was not; ``unshare WORKER --unit SLUG`` takes the unit back, and with
the last one sharing ends. WORKER is a worker's name, or OWNER/NAME when a hub admin sees two of that name. A refusal
of the hub prints its reason and exits 1.

Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.contract import json_option, returns_array, returns_object

EXIT_UNKNOWN = 1  # as a 404 of the hub exits
WORKER_SHARE_KEYS = ("id", "worker", "owner", "shared", "units", "doctor_at", "doctor_high", "doctor_medium")


class Unknown(Exception):
    """A worker name the caller's list does not hold, or holds twice."""


def find_worker(rows: list[dict], name: str) -> dict:
    """The row of ``name`` (a worker's name, or OWNER/NAME) among the workers the caller may share."""
    owner, _, bare = name.rpartition("/")
    found = [row for row in rows if row["worker"] == bare and owner in ("", row["owner"])]
    if not found:
        raise Unknown(f"you share no live worker named {name}: see `evo-agents hub org worker list`")
    if len(found) > 1:
        names = ", ".join(f"{row['owner']}/{row['worker']}" for row in found)
        raise Unknown(f"{len(found)} workers are named {bare}: name one as OWNER/NAME ({names})")
    return found[0]


def _doctor(row: dict) -> str:
    if row.get("doctor_at") is None:
        return "no doctor report"
    return f"doctor {row['doctor_at']}: {row['doctor_high']} high, {row['doctor_medium']} medium"


def worker_line(row: dict) -> str:
    if not row["shared"]:
        whom = "not shared"
    elif row["units"]:
        whom = "shared with " + ", ".join(row["units"])
    else:
        whom = "shared with every writer"
    return f"{row['owner']}/{row['worker']}  {whom}  ({_doctor(row)})"


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    rows = hub.call("GET", "/v1/org/workers")
    if args.json:
        print_json(rows)
        return 0
    print("\n".join(worker_line(row) for row in rows) if rows else "You have no live worker.")
    return 0


def _change(args, method: str):
    hub, _ = signed_in()
    try:
        row = find_worker(hub.call("GET", "/v1/org/workers"), args.worker)
    except Unknown as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return hub.call(method, f"/v1/org/workers/{row['id']}/units/{quote(args.unit, safe='')}")


@client_command
def cmd_share(args) -> int:
    row = _change(args, "PUT")
    if row is None:
        return EXIT_UNKNOWN
    if args.json:
        print_json(row)
        return 0
    print(f"Worker {row['owner']}/{row['worker']} is shared with {', '.join(row['units'])}.")
    return 0


@client_command
def cmd_unshare(args) -> int:
    row = _change(args, "DELETE")
    if row is None:
        return EXIT_UNKNOWN
    if args.json:
        print_json(row)
        return 0
    print(f"Took unit {args.unit} back from worker {row['owner']}/{row['worker']}: {worker_line(row)}")
    return 0


def register_workers(osub) -> None:
    group = osub.add_parser("worker", help="whom your shared workers are shared with: units of the org chart")
    wsub = group.add_subparsers(dest="org_worker_command", required=True)

    listed = wsub.add_parser("list", help="your live workers (every one for a hub admin) and whom each is shared with")
    json_option(listed, returns_array(*WORKER_SHARE_KEYS, schema="WorkerShares"))
    listed.set_defaults(func=cmd_list)

    for name, verb, func in (
        ("share", "narrow a worker to a unit and every unit below it, sharing it when it was not", cmd_share),
        ("unshare", "take a unit back from a worker; with its last unit, sharing ends", cmd_unshare),
    ):
        parser = wsub.add_parser(name, help=verb)
        parser.add_argument("worker", metavar="WORKER", help="the worker's name, or OWNER/NAME")
        parser.add_argument("--unit", required=True, metavar="SLUG", help="the unit of the org chart")
        json_option(parser, returns_object(*WORKER_SHARE_KEYS, schema="WorkerShares"))
        parser.set_defaults(func=func)
