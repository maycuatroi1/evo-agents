"""``evo-agents hub kg entity-map``: the map of a project's entities to its code, for strategy entity-graph-agent.

``entity-map --project P`` lists the project's entity map runs, newest first, with the one in flight (GET
/v1/kg/{project}/entity-map/runs). ``--run`` asks the hub for a run now (POST .../entity-map/run, the writer role): a
review run with lens kg_entity_map on the project's default agent pool, under the charter's review cost cap
(``evo_agents.hub.server.kg_entity_map_run``); asked again while one is in flight, the hub answers with that run.

What the agent of that run calls: ``--outline`` prints the structure of the project as the run's sink reads the latest
graph (the repos and directories with their files and symbols, the clusters that hold code, the entities, the
documents and the plans); ``--changed`` the items new, changed or gone since the last map and what no map cites;
``--post FILE`` sends a map of format evo-entity-map/1 (``evo_agents.kg.entity_map``), which the hub checks and keeps
before it queues a build. ``--json`` prints the hub's answer. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.contract import json_option, returns_object

RUN = (
    "id",
    "project",
    "via",
    "state",
    "why",
    "build_id",
    "agent_run_id",
    "maps",
    "readable",
    "notes",
    "error",
    "created_at",
    "updated_at",
    "ended_at",
)
RUNS = ("active", "runs")
OUTLINE = ("project", "sink", "counts", "repos", "directories", "clusters", "entities", "documents", "plans")
CHANGED = ("project", "sink", "since_map", "counts", "new", "changed", "gone", "uncited")
KEPT = ("entities", "relations", "mentions", "code_links", "rejected", "sha256", "queued")


def _path(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}/entity-map"


def _line(run: dict) -> str:
    review = f", review run {run['agent_run_id']}" if run.get("agent_run_id") else ""
    error = f": {run['error']}" if run.get("error") else ""
    return f"#{run['id']} {run['via']} {run['state']}{review}, {run['maps']} maps posted{error}"


def _map(path: str) -> dict:
    try:
        found = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        sys.exit(f"cannot read the map file {path}: {exc}")
    except ValueError as exc:
        sys.exit(f"the map file {path} is not JSON: {exc}")
    if not isinstance(found, dict):
        sys.exit(f"the map file {path} holds no JSON object")
    return found


def _post(hub, args) -> int:
    made = hub.call("POST", _path(args.project), _map(args.post))
    if args.json:
        print_json(made)
        return 0
    print(
        f"entity map of project {args.project}: kept {made['entities']} entities, {made['relations']} relations, "
        f"{made['mentions']} citations and {made['code_links']} code links"
        + (", a build is queued" if made["queued"] else "")
    )
    for refused in made["rejected"]:
        print(f"  rejected {refused['entry']}: {refused['reason']}")
    return 0


def _counts(counts: dict) -> str:
    return ", ".join(f"{n} {name}" for name, n in counts.items())


@client_command
def cmd_entity_map(args) -> int:
    hub, _ = signed_in()
    if args.post:
        return _post(hub, args)
    if args.outline:
        found = hub.call("GET", f"{_path(args.project)}/outline")
        if args.json:
            print_json(found)
            return 0
        print(f"project {args.project} through sink {found['sink']}: {_counts(found['counts'])}")
        for d in found["directories"]:
            print(f"  {d['repo']}:{d['path']} {d['files']} files, {d['symbols']} symbols")
        return 0
    if args.changed:
        found = hub.call("GET", f"{_path(args.project)}/changed?limit={args.limit or 500}")
        if args.json:
            print_json(found)
            return 0
        since = "since the last map" if found["since_map"] else "no map yet"
        print(f"project {args.project} ({since}): {_counts(found['counts'])}")
        for key in ("new", "changed", "gone", "uncited"):
            for item in found[key]:
                print(f"  {key} {item['id']}")
        return 0
    if args.run:
        found = hub.call("POST", f"{_path(args.project)}/run", {})
        if args.json:
            print_json(found)
            return 0
        print(f"entity map run of project {args.project}: {_line(found)}")
        return 0
    found = hub.call("GET", f"{_path(args.project)}/runs?limit={min(args.limit or 10, 50)}")
    if args.json:
        print_json(found)
        return 0
    if not found["runs"]:
        print(f"project {args.project} has no entity map run yet")
    for run in found["runs"]:
        print(_line(run))
    return 0


def register(ksub) -> None:
    from evo_agents.hub.cli import positive

    parser = ksub.add_parser(
        "entity-map",
        help="map a project's entities to its code (strategy entity-graph-agent): its runs, or what a run reads and "
        "posts",
    )
    parser.add_argument("--project", required=True, help="project name on the hub")
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--run", action="store_true", help="ask for an entity map run now (the writer role)")
    what.add_argument(
        "--outline",
        action="store_true",
        help="the project's structure as the run's sink reads the graph: repos, directories with their files and "
        "symbols, code clusters, entities, documents and plans",
    )
    what.add_argument(
        "--changed", action="store_true", help="the items new or changed since the last map, and what no map cites"
    )
    what.add_argument("--post", metavar="FILE", help="send an entity map (JSON, format evo-entity-map/1) from a run")
    parser.add_argument(
        "--limit", type=positive, help="the newest runs listed (10); with --changed, the items of each list (500)"
    )
    json_option(
        parser,
        returns_object(
            *RUNS,
            schema="EntityMapRuns",
            variants=(
                ("--run", returns_object(*RUN, schema="EntityMapRun")),
                ("--outline", returns_object(*OUTLINE, schema="EntityMapOutline")),
                ("--changed", returns_object(*CHANGED, schema="EntityMapChanged")),
                ("--post", returns_object(*KEPT, schema="MapKept")),
            ),
        ),
        help="machine-readable output: the runs, with --run the run started or in flight, with --outline the "
        "structure, with --changed what changed, with --post what the hub kept",
    )
    parser.set_defaults(func=cmd_entity_map)
