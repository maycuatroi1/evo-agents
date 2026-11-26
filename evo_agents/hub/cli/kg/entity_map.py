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

:data:`ENTITY_MAP` is a class command of ``evo_agents.clikit``; :class:`KgEntityMap` is the gateway to its routes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evo_agents.clikit import Result, Text, returns_object
from evo_agents.hub.cli import positive
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource

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
CHANGED_LIMIT = 500  # the items of each list --changed gives by default
RUNS_LIMIT, MAX_RUNS = 10, 50  # the runs listed by default, and at most


class KgEntityMap(KgResource):
    """The entity map of a project: POST /v1/kg/{p}/entity-map, GET .../outline, .../changed and .../runs, POST
    .../run. Every route but the post answers a JSON object; the command exits with a message when one did not."""

    collection = "entity-map"

    def ask(self, method: str, *parts: str, body: dict | None = None, **query) -> dict:
        found = self.call(method, *parts, body=body, **query)
        if not isinstance(found, dict):
            sys.exit(f"the hub answered {method} {self.path(*parts, **query)} with no JSON object")
        return found

    def post(self, entity_map: dict) -> dict:
        return self.call("POST", body=entity_map)

    def outline(self) -> dict:
        return self.ask("GET", "outline")

    def changed(self, limit: int) -> dict:
        return self.ask("GET", "changed", limit=limit)

    def start(self) -> dict:
        return self.ask("POST", "run", body={})

    def runs(self, limit: int) -> dict:
        return self.ask("GET", "runs", limit=limit)


def run_line(run: dict) -> str:
    review = f", review run {run['agent_run_id']}" if run.get("agent_run_id") else ""
    error = f": {run['error']}" if run.get("error") else ""
    return f"#{run['id']} {run['via']} {run['state']}{review}, {run['maps']} maps posted{error}"


def map_file(path: str) -> dict:
    """The entity map in the JSON file ``path``; the command exits with a message when it holds none."""
    try:
        found = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        sys.exit(f"cannot read the map file {path}: {exc}")
    except ValueError as exc:
        sys.exit(f"the map file {path} is not JSON: {exc}")
    if not isinstance(found, dict):
        sys.exit(f"the map file {path} holds no JSON object")
    return found


def counts_text(counts: dict) -> str:
    return ", ".join(f"{n} {name}" for name, n in counts.items())


def posted_lines(project: str, made: dict) -> list[str]:
    return [
        f"entity map of project {project}: kept {made['entities']} entities, {made['relations']} relations, "
        f"{made['mentions']} citations and {made['code_links']} code links"
        + (", a build is queued" if made["queued"] else ""),
        *(f"  rejected {refused['entry']}: {refused['reason']}" for refused in made["rejected"]),
    ]


def outline_lines(project: str, found: dict) -> list[str]:
    return [
        f"project {project} through sink {found['sink']}: {counts_text(found['counts'])}",
        *(f"  {d['repo']}:{d['path']} {d['files']} files, {d['symbols']} symbols" for d in found["directories"]),
    ]


def changed_lines(project: str, found: dict) -> list[str]:
    since = "since the last map" if found["since_map"] else "no map yet"
    lines = [f"project {project} ({since}): {counts_text(found['counts'])}"]
    for key in ("new", "changed", "gone", "uncited"):
        lines += [f"  {key} {item['id']}" for item in found[key]]
    return lines


class KgEntityMapCommand(HubCommand):
    name = "entity-map"
    help = "map a project's entities to its code (strategy entity-graph-agent): its runs, or what a run reads and posts"
    arguments = (KG_PROJECT,)
    output = returns_object(
        *RUNS,
        schema="EntityMapRuns",
        variants=(
            ("--run", returns_object(*RUN, schema="EntityMapRun")),
            ("--outline", returns_object(*OUTLINE, schema="EntityMapOutline")),
            ("--changed", returns_object(*CHANGED, schema="EntityMapChanged")),
            ("--post", returns_object(*KEPT, schema="MapKept")),
        ),
    )
    json_help = (
        "machine-readable output: the runs, with --run the run started or in flight, with --outline the "
        "structure, with --changed what changed, with --post what the hub kept"
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
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
        what.add_argument(
            "--post", metavar="FILE", help="send an entity map (JSON, format evo-entity-map/1) from a run"
        )
        parser.add_argument(
            "--limit", type=positive, help="the newest runs listed (10); with --changed, the items of each list (500)"
        )

    def run(self, ctx: HubContext) -> Result:
        args, project, entity_map = ctx.args, ctx.project, KgEntityMap.of(ctx)
        if args.post:
            made = entity_map.post(map_file(args.post))
            return Result(made, Text(*posted_lines(project, made)))
        if args.outline:
            found = entity_map.outline()
            return Result(found, Text(*outline_lines(project, found)))
        if args.changed:
            found = entity_map.changed(args.limit or CHANGED_LIMIT)
            return Result(found, Text(*changed_lines(project, found)))
        if args.run:
            found = entity_map.start()
            return Result(found, Text(f"entity map run of project {project}: {run_line(found)}"))
        found = entity_map.runs(min(args.limit or RUNS_LIMIT, MAX_RUNS))
        lines = [run_line(run) for run in found["runs"]] or [f"project {project} has no entity map run yet"]
        return Result(found, Text(*lines))


ENTITY_MAP = KgEntityMapCommand()
NODES = (ENTITY_MAP,)
