"""``evo-agents hub kg init``: have the Curator configure a project's knowledge graph from its data profile.

``init --project P`` asks the hub for a kg_init run (POST /v1/kg/{project}/init, the writer role): the hub measures the
starter questions, gives the Curator the data profile, the build's coverage, the strategies and the measures as a review
run on the project's default agent pool, and the Curator decides and applies without waiting for a person
(``evo_agents.hub.server.kg_init``). One run of a project is in flight at a time: asked again, the hub answers with that
run. ``--all`` (a hub admin) asks for every project with a knowledge config. ``--status`` lists the project's runs,
newest first, each with its state and decisions (GET). ``--decide FILE`` is what the Curator's agent runs to record its
decision, a JSON object (``evo_agents.kg.init_run``), in the run in flight or ``--run ID``. ``--questions FILE`` is what
the agent of the run that writes the starter questions (lens kg_questions) runs to send them, one JSON object a line or a
JSON array; the hub checks every answer against the graph before it keeps them (``evo_agents.hub.server.kg_questions``).
``--json`` prints the hub's answer. Standard library only.
"""

from __future__ import annotations

import sys
from pathlib import Path

from evo_agents.clikit import Arg, Result, Text, returns_object
from evo_agents.hub.cli import positive
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.kg_routes import InitRuns, KgAdmin

__all__ = ["KG_INIT", "line_of"]

INIT_RUN = (
    "id",
    "project",
    "via",
    "state",
    "why",
    "build_id",
    "agent_run_id",
    "questions",
    "summary",
    "decisions",
    "notes",
    "report",
    "error",
    "created_at",
    "updated_at",
    "ended_at",
)
INIT_RUNS = ("active", "runs")
INIT_ALL = ("started", "skipped")
INIT_DECISION = ("run", "applied")
INIT_QUESTIONS = ("run", "set", "version", "questions", "holdout", "classes")


def line_of(run: dict) -> str:
    review = f", review run {run['agent_run_id']}" if run.get("agent_run_id") else ""
    asked = run.get("questions") or {}
    if asked.get("state"):
        review += f", questions {asked['state']}" + (
            f" (run {asked['agent_run_id']})" if asked.get("agent_run_id") else ""
        )
    return f"#{run['id']} {run['via']} {run['state']}{review}: {run['summary']}"


def _questions(path: str) -> list:
    """The questions of ``path``: a JSON array, or one JSON object a line."""
    import json

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(f"cannot read the questions file {path}: {exc}")
    try:
        found = json.loads(text)
        return found if isinstance(found, list) else [found]
    except ValueError:
        pass
    try:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError as exc:
        sys.exit(f"the questions file {path} is neither a JSON array nor one JSON object a line: {exc}")


def _decision(path: str) -> dict:
    from evo_agents.kg import init_run

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(f"cannot read the decision file {path}: {exc}")
    try:
        return init_run.extract_json(text)
    except init_run.DecisionError as exc:
        sys.exit(str(exc))


class KgInit(HubCommand):
    name = "init"
    help = "have the Curator configure a project's knowledge graph from its data profile, or read its runs"
    arguments = (
        Arg("--project", help="project name on the hub"),
        Arg("--all", action="store_true", help="every project with a knowledge config (a hub admin)"),
        Arg("--status", action="store_true", help="list the project's runs instead of starting one"),
        Arg("--limit", type=positive, default=10, help="with --status: the newest runs listed"),
        Arg("--decide", metavar="FILE", help="record the Curator's decision (a JSON file) in a run"),
        Arg(
            "--questions",
            metavar="FILE",
            help="send the starter questions (JSON lines) of a kg init run's questions run",
        ),
        Arg("--run", type=positive, help="with --decide or --questions: the kg init run (default: the one in flight)"),
    )
    output = returns_object(
        *INIT_RUN,
        schema="InitRun",
        variants=(
            ("--status", returns_object(*INIT_RUNS, schema="InitRuns")),
            ("--all", returns_object(*INIT_ALL, schema="InitAll")),
            ("--decide", returns_object(*INIT_DECISION, schema="DecisionOut")),
            ("--questions", returns_object(*INIT_QUESTIONS, schema="QuestionsOut")),
        ),
    )
    json_help = (
        "machine-readable output: the run started or in flight, with --status the runs, with --all the "
        "projects asked, with --decide what the decision applied, with --questions the eval set version kept"
    )

    def run(self, ctx: HubContext) -> Result:
        args, hub = ctx.args, ctx.hub  # signed in first, as before
        if args.all:
            return self.all(KgAdmin(hub))
        if not args.project:
            sys.exit("name a project with --project, or --all for every project (a hub admin)")
        runs = InitRuns(hub, args.project)
        if args.questions:
            return self.send_questions(runs, args)
        if args.decide:
            return self.decide(runs, args)
        if args.status:
            return self.status(runs, args)
        found = runs.start()
        return Result(found, Text(f"kg init run of project {args.project}: {line_of(found or {})}"))

    @staticmethod
    def all(admin: KgAdmin) -> Result:
        found = admin.init_all()
        lines = [f"kg init asked for {len(found['started'])} projects: {', '.join(found['started']) or 'none'}"]
        lines += [f"  left out {name}: {why}" for name, why in sorted(found["skipped"].items())]
        return Result(found, Text(*lines))

    @staticmethod
    def in_flight(runs: InitRuns, args) -> int:
        """``--run``, else the run in flight; it exits when there is none."""
        found = runs.list(args.limit)
        run = args.run or (found.get("active") or {}).get("id")
        if run is None:
            sys.exit(f"project {args.project} has no kg init run in flight; pass --run ID")
        return run

    def send_questions(self, runs: InitRuns, args) -> Result:
        run = self.in_flight(runs, args)
        made = runs.send_questions(run, _questions(args.questions))
        return Result(
            made,
            Text(
                f"kg init run #{made['run']} of project {args.project}: {made['questions']} starter questions kept as "
                f"version {made['version']} of set {made['set']} ({made['holdout']} in holdout)"
            ),
        )

    def decide(self, runs: InitRuns, args) -> Result:
        run = self.in_flight(runs, args)
        made = runs.decide(run, _decision(args.decide))
        lines = [f"kg init run #{made['run']} of project {args.project} decided:"]
        lines += [f"  {part}: {what}" for part, what in sorted(made["applied"].items())]
        return Result(made, Text(*lines))

    @staticmethod
    def status(runs: InitRuns, args) -> Result:
        found = runs.list(args.limit)
        lines = [] if found["runs"] else [f"project {args.project} has no kg init run yet"]
        return Result(found, Text(*lines, *(line_of(run) for run in found["runs"])))


KG_INIT = KgInit()
