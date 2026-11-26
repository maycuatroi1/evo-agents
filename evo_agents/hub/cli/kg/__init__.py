"""``evo-agents hub kg push|builds|build|health|forget-source|prune|scrub|clusters...``: knowledge graphs on the hub.

``push`` sends the runs a project's corpus holds and the hub lacks (``evo_agents.hub.kg_push``): ``--project P`` one
project of this machine, ``--all`` every project of projects.json the hub takes a push for. It reports its progress on
stderr, a line per run and per batch of blobs, and its outcome on stdout. ``builds`` lists a project's builds and the
jobs still queued; ``build`` (the writer role) queues one and with ``--wait`` waits until it has finished, exiting 1
when it failed. ``health`` prints the health of a project's graph over time as the hub measures it
(``evo_agents.hub.server.kg_health``): for each git source of the latest measure the commit read, how many commits of
the default branch it lacks and for how long, the files missing, stale and deleted, then the retrieval evaluation and
the builds that changed nothing. ``forget-source`` (the writer role) takes every run of one or more sources off the
hub and queues a build without them (``evo_agents.hub.server.kg``), which a source must go through before
knowledge.yaml drops it; at a terminal it asks first unless ``--yes``. ``prune`` (a hub admin) runs the retention of
built graphs now (``evo_agents.hub.kg_prune``): the artifacts of graphs older than each project's ``--keep`` newest
leave the bucket, and ``--dry-run`` only says which.
``scrub`` (a hub admin) reads the graph in the hub's tables and every graph file and source blob of a project, or of
every project, page by page (``evo_agents.hub.server.kg_scrub``) and lists those holding a secret by hash, kind and the
kinds of secret, never a value; ``--purge`` says what it is about to delete, asks (or takes ``--yes``), and has the hub
delete the old ones.
``clusters`` lists the clusters of a project's graph the caller can see, and ``clusters show ID`` one of them
(``evo_agents.hub.cli.kg.clusters``). ``review`` lists the review queue the caller can see, and ``review dismiss FP``
dismisses one item of it (``evo_agents.hub.cli.kg.review``). ``entities``, inside a run of the project's agent pool,
answers with Claude Code the entity requests of its latest build (``evo_agents.hub.cli.kg.entities``). ``decisions``
lists the decisions on the graph the caller can see, and a writer takes or steps one (``evo_agents.hub.cli.kg.decisions``).

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes), listed by hand in
:data:`KG`; the hub's routes they ask are the gateways of ``evo_agents.hub.kg_routes``. The hub backend of ``kg serve``
and ``kg query`` (``open_session``) and the projects a push sends are in ``evo_agents.hub.kg_remote``. Standard library
only.
"""

from __future__ import annotations

# settings, listed in KG below, imports MEASURE_TIMEOUT from this package: it is bound before the modules are imported
from evo_agents.hub.kg_routes import MEASURE_TIMEOUT

# isort: split
import sys

from evo_agents.clikit import Arg, Group, Result, Stderr, Table, Text, UsageError, View, Views, returns_object
from evo_agents.hub.cli import positive
from evo_agents.hub.cli.base import HubCommand, HubContext, when
from evo_agents.hub.cli.kg import (
    answer,
    branches,
    clusters,
    decisions,
    entities,
    entity_map,
    eval_set,
    graphs,
    init,
    optimize,
    profile,
    review,
    semantic,
    settings,
    upload,
)
from evo_agents.hub.client import HubError
from evo_agents.hub.kg_push import PushReport
from evo_agents.hub.kg_remote import push, push_json
from evo_agents.hub.kg_routes import Builds, GraphHealth, KgAdmin, Sources

__all__ = ["KG", "MEASURE_TIMEOUT"]

DEFAULT_WAIT = 1800.0
SCRUB_SHOWN = 20  # objects listed per project in text; --json lists them all
HUB_PROJECT = "project name on the hub"


def stderr_line(line: str) -> None:
    """A line of progress: stderr, as it comes, since stdout keeps the summary, or the JSON of --json."""
    print(line, file=sys.stderr, flush=True)


def confirmed(ctx: HubContext, about: list[str], question: str) -> bool:
    """Whether the person at the terminal answers yes to ``question``, after the lines of ``about``."""
    for line in about:
        print(line, file=ctx.out)
    return input(question).strip().lower() in ("y", "yes")


def _bytes(size: int) -> str:
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


# Pushing


def report_lines(reports: list[PushReport]) -> list[str]:
    """Each project's summary line and its errors."""
    return [line for report in reports for line in (report.summary_line(), *(f"  error: {e}" for e in report.errors))]


class KgPush(HubCommand):
    name = "push"
    help = "send the runs of a project's corpus that the hub does not have"
    arguments = (
        Arg("--project", help="project name or harness path"),
        Arg("--all", action="store_true", help="every project in projects.json the hub takes a push for from you"),
    )
    output = returns_object("ok", "projects")

    def check(self, args) -> None:
        if args.all == bool(args.project):
            raise HubError("pass --project P or --all")

    def run(self, ctx: HubContext) -> Result:
        from evo_agents.kg.project import ProjectError

        try:
            reports = push(ctx.hub, ctx.args.project, ctx.args.all, stderr_line)
        except ProjectError as exc:
            raise HubError(str(exc)) from None
        return Result(push_json(reports), Text(*report_lines(reports)), 0 if all(r.ok for r in reports) else 1)


# Builds


def _artifact_note(build: dict) -> str:
    """What became of a succeeded build's artifact, when it is not simply its own."""
    if build.get("artifact_pruned_at"):
        return ", artifact pruned"
    if build.get("artifact_reused_from"):
        return f", artifact of build {build['artifact_reused_from']}"
    return ""


def _counts(build: dict) -> str:
    """A succeeded build's totals; the hub shows none of a graph that holds chats, whose kg_status counts instead."""
    if build.get("nodes") is None or build.get("edges") is None:
        return "counts in kg_status"
    return f"{build['nodes']} nodes, {build['edges']} edges"


def _build_row(build: dict) -> tuple:
    succeeded = build["status"] == "succeeded"
    outcome = build.get("error") or (f"{_counts(build)}{_artifact_note(build)}" if succeeded else "")
    return (
        build["id"],
        build["status"],
        when(build.get("queued_at")),
        when(build.get("finished_at")),
        build.get("runs") if build.get("runs") is not None else "-",
        (build.get("content_hash") or "-")[:19],
        outcome[:80],
    )


def _job_line(job: dict) -> str:
    state = "running" if job["status"] == "doing" else "waiting"
    return f"job {job['job_id']} {state}" + (f" (build {job['build_id']})" if job["build_id"] else "")


class KgBuilds(HubCommand):
    name = "builds"
    help = "a project's builds on the hub, newest first, and the queued jobs"
    arguments = (
        Arg("--project", required=True, help=HUB_PROJECT),
        Arg("--limit", type=int, default=20, help="builds to show (default: 20, at most 100)"),
    )
    output = returns_object("builds", "jobs", schema="Builds")
    columns = ("BUILD", "STATUS", "QUEUED (UTC)", "FINISHED (UTC)", "RUNS", "CONTENT HASH", "OUTCOME")

    def run(self, ctx: HubContext) -> Result:
        found = Builds(ctx.hub, ctx.project).list(ctx.args.limit)
        rows = [_build_row(build) for build in found["builds"]]
        listed = Table(self.columns, rows, f"project {ctx.project} has no build on the hub yet")
        return Result(found, Views(listed, Text(*(_job_line(job) for job in found["jobs"]))))


class KgBuild(HubCommand):
    name = "build"
    help = "queue a build of a project's graph (needs the writer role)"
    arguments = (
        Arg("--project", required=True, help=HUB_PROJECT),
        Arg("--wait", action="store_true", help="wait until the build has finished; exit 1 if it failed"),
        Arg("--timeout", type=float, default=DEFAULT_WAIT, help="seconds --wait waits at most (default: 1800)"),
    )
    output = returns_object("queued", "build", schema="Queued")

    def run(self, ctx: HubContext) -> Result:
        builds = Builds(ctx.hub, ctx.project)
        queued = builds.queue()
        build = queued.get("build")
        if build is None:
            raise HubError(f"a build of project {ctx.project} is waiting already, but the hub names none")
        if ctx.args.wait:
            build = builds.wait(build["id"], ctx.args.timeout)
        verb = "Queued" if queued["queued"] else "A build was waiting already:"
        parts: list[View] = [Text(f"{verb} build {build['id']} of project {ctx.project}: {build['status']}.")]
        if build["status"] == "succeeded":
            parts.append(Text(f"{_counts(build)}, content {build['content_hash']}{_artifact_note(build)}"))
        elif build["status"] == "failed":
            parts.append(Stderr(Text(f"error: {build['error']}")))
        status = 1 if build["status"] == "failed" else 0
        return Result({"queued": queued["queued"], "build": build}, Views(*parts), status)


# Health


def _count(value) -> str:
    return "-" if value is None else str(value)


def _source_row(source: dict) -> tuple:
    lag = source.get("lag_hours")
    return (
        source.get("source"),
        (source.get("commit") or "-")[:12],
        _count(source.get("behind")),
        "-" if lag is None else f"{lag:g}",
        _count(source.get("missing")),
        _count(source.get("stale")),
        _count(source.get("deleted")),
        (source.get("why") or "")[:80],
    )


def _eval_line(found: dict | None) -> str:
    if not isinstance(found, dict) or not found.get("methods"):
        return f"retrieval eval: {(found or {}).get('why') or 'no question'}"
    row = found["methods"].get("kg_search") or {}
    asked = f"{found.get('questions')} questions"
    if found.get("set"):
        asked += (
            f" ({found['set'].get('holdout')} holdout of set {found['set'].get('name')} v{found['set'].get('version')})"
        )
    skipped = found.get("skipped") or 0
    dropped = f", {skipped} step(s) left out" if skipped else ""
    return (
        f"retrieval eval of build {found.get('build_id')}: {asked}{dropped}, kg_search hit@5 {row.get('hit@5')}, "
        f"R@5 {row.get('recall@5')}, MRR {row.get('mrr')}, empty {row.get('empty')}, "
        f"tok/file {row.get('tokens_per_file')}"
    )


class KgHealth(HubCommand):
    name = "health"
    help = "the health of a project's graph over time, as the hub measures it"
    arguments = (
        Arg("--project", required=True, help=HUB_PROJECT),
        Arg("--limit", type=int, default=30, help="measures and builds to show (default: 30, at most 200)"),
    )
    output = returns_object("project", "measures", "builds", schema="KgHealthView")
    columns = ("SOURCE", "COMMIT", "BEHIND", "LAG (H)", "MISSING", "STALE", "DELETED", "WHY")

    def run(self, ctx: HubContext) -> Result:
        found = GraphHealth(ctx.hub, ctx.project).measures(ctx.args.limit)
        if not found["measures"]:
            return Result(
                found,
                Text(
                    f"the hub has not measured the graph of project {ctx.project} yet: curator.collect does, every 6 hours"
                ),
            )
        return Result(found, self.measured(found))

    def measured(self, found: dict) -> View:
        latest = found["measures"][0]
        parts: list[View] = [
            Text(f"Measured {when(latest['measured_at'])} (UTC), build {latest.get('build_id') or '-'}:")
        ]
        if latest.get("error"):
            parts.append(Stderr(Text(f"error: {latest['error']}")))
        parts.append(Table(self.columns, [_source_row(source) for source in latest["sources"]]))
        builds = found["builds"]
        unchanged = sum(1 for build in builds if not build["changed"])
        parts.append(
            Text(
                _eval_line(latest.get("eval")),
                f"{len(builds)} successful builds listed, {unchanged} of them changed nothing",
            )
        )
        return Views(*parts)


# Cleaning up


def forget_json(project: str, found: list[dict]) -> dict:
    """What ``hub kg forget-source --json`` prints."""
    return {"project": project, "sources": found}


def forget_line(project: str, found: dict) -> str:
    runs, uploads = found["ingested"] + found["pending"], found.get("uploads", 0)
    if not runs and not uploads:
        return f"the hub holds no run of source {found['source']} of project {project}; nothing changed"
    line = f"took {runs} run(s) of source {found['source']} off project {project}"
    if uploads:
        line += f"; deleted {uploads} uploaded archive(s) of it"
    if found["logs"]:
        line += f"; {found['logs']} log(s) leave the blob store with the next prune"
    build = found.get("build")
    if build:
        line += f"; build {build['id']} {build['status']}"
    return line


class KgForgetSource(HubCommand):
    name = "forget-source"
    help = "take every run of a source off the hub, so knowledge.yaml can drop it (needs the writer role)"
    arguments = (
        Arg("--project", required=True, help=HUB_PROJECT),
        Arg("--source", required=True, action="append", help="the source's id in knowledge.yaml; repeat for several"),
        Arg("--yes", action="store_true", help="do not ask first at a terminal"),
    )
    output = returns_object("project", "sources")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        sources_of = Sources(ctx.hub, ctx.project)  # signed in before the question
        sources = list(dict.fromkeys(args.source))
        if not args.yes and not args.json and sys.stdin.isatty():
            about = (
                f"About to take every run of {', '.join(sources)} off project {ctx.project} on the hub; the graph is "
                "built again without them."
            )
            if not confirmed(ctx, [about], "Go on? [y/N] "):
                return Result(None, Text("Nothing changed."), 1)
        found = sources_of.forget(sources)
        parts: list[View] = [Text(*(forget_line(ctx.project, one) for one in found))]
        still = [one["source"] for one in found if one["declared"] and one["ingested"] + one["pending"]]
        if still:
            parts.append(
                Stderr(
                    Text(
                        f"warning: the knowledge config on the hub still declares {', '.join(still)}: remove it from "
                        "knowledge.yaml and push (`evo-agents kg sync --push`), or a machine holding its runs sends "
                        "them again"
                    )
                )
            )
        return Result(forget_json(ctx.project, found), Views(*parts))


class KgPrune(HubCommand):
    name = "prune"
    help = "delete the artifacts of graphs older than each project's newest few (needs a hub admin)"
    arguments = (
        Arg("--project", help="project name on the hub (default: every project)"),
        Arg(
            "--keep",
            type=positive,
            help="newest graphs of each project whose artifact stays (default: the hub's EVO_HUB_KG_KEEP_ARTIFACTS, 3)",
        ),
        Arg("--dry-run", action="store_true", help="say what would be deleted, and delete nothing"),
        Arg(
            "--graph-files",
            action="store_true",
            help="also delete every graph file (.sqlite) of a project whose graph the hub's tables hold, the newest too",
        ),
    )
    output = returns_object(
        "dry_run", "keep", "graph_files", "projects", "deleted", "deleted_bytes", "pending", schema="KgPruned"
    )
    columns = ("PROJECT", "ARTIFACTS", "KEPT", "PRUNED", "PRUNED SIZE", "BUILDS")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        report = KgAdmin(ctx.hub).prune(args.project, args.keep, args.dry_run, args.graph_files)
        rows = [
            (p["project"], p["artifacts"], p["kept"], p["pruned"], _bytes(p["pruned_bytes"]), p["builds"])
            for p in report["projects"]
        ]
        verb = "would delete" if report["dry_run"] else "deleted"
        parts: list[View] = [
            Table(self.columns, rows, "no project has a built graph on the hub"),
            Text(
                f"{'Dry run, keeping' if report['dry_run'] else 'Kept'} the artifacts of the {report['keep']} newest "
                f"graph(s) of each project: {verb} {report['deleted']} object(s), {_bytes(report['deleted_bytes'])}."
            ),
        ]
        if report["pending"]:
            parts.append(
                Stderr(Text(f"{report['pending']} object(s) still wait to be deleted; the next prune tries again."))
            )
        return Result(report, Views(*parts))


# Old copies of secrets


def scrub_json(purge: bool, reports: list[dict]) -> dict:
    """What ``hub kg scrub --json`` prints."""
    return {"purge": purge, "projects": reports}


def _old(reports: list[dict]) -> list[dict]:
    return [found for report in reports for found in report["found"] if found["state"] == "old"]


def _secrets(secrets: dict) -> str:
    return ", ".join(f"{kind} {count}" for kind, count in secrets.items())


def scrub_lines(report: dict) -> list[str]:
    """What one project's scrub found, its first SCRUB_SHOWN objects listed."""
    found = report["found"]
    states = {state: sum(1 for f in found if f["state"] == state) for state in ("old", "live")}
    lines = [
        f"{report['project']}: {len(found)} of {report['scanned']} object(s) hold a secret: {states['old']} old, "
        f"{states['live']} live source blob(s)"
        + (f"; {report['missing']} missing from the bucket" if report["missing"] else "")
    ]
    graph = report.get("graph")
    if graph and graph["holding"]:
        lines.append(
            f"  in use  graph     {graph['holding']} of {graph['rows']} row(s) in the hub's tables  "
            f"{_secrets(graph['secrets'])}: push runs with them redacted and build again; a purge never deletes them"
        )
    for item in found[:SCRUB_SHOWN]:
        lines.append(
            f"  {item['state']:<6}  {item['kind']:<8}  {item['sha256'][:16]}  {_bytes(item['size'])}  "
            f"{_secrets(item['secrets'])}"
        )
    if len(found) > SCRUB_SHOWN:
        lines.append(f"  and {len(found) - SCRUB_SHOWN} more: --json lists them all")
    return lines


def purged_view(report: dict) -> View:
    """What the purge of one project deleted and kept."""
    purged = report["purged"]
    reasons: dict[str, int] = {}
    for kept in purged["kept"]:
        reasons[kept["reason"]] = reasons.get(kept["reason"], 0) + 1
    kept = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
    deleted = Text(
        f"{report['project']}: deleted {len(purged['purged'])} object(s), {_bytes(purged['deleted_bytes'])} left the "
        f"bucket; {purged['builds']} build(s) lost their graph" + (f"; kept {kept}" if kept else "")
    )
    if not purged["pending"]:
        return deleted
    return Views(
        deleted, Stderr(Text(f"{purged['pending']} object(s) still wait to be deleted; the next prune tries again."))
    )


class KgScrub(HubCommand):
    name = "scrub"
    help = "find old graphs and source blobs holding a secret, and with --purge delete them (a hub admin)"
    arguments = (
        Arg("--project", help="project name on the hub (default: every project)"),
        Arg("--purge", action="store_true", help="delete the old ones; the graph in the tables and live blobs stay"),
        Arg("--yes", action="store_true", help="with --purge: delete without asking"),
    )
    output = returns_object("purge", "projects")

    def check(self, args) -> None:
        if args.purge and not args.yes and (args.json or not sys.stdin.isatty()):
            raise UsageError("--purge deletes for good; pass --yes too when no terminal can confirm it")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        admin = KgAdmin(ctx.hub)
        names = [args.project] if args.project else admin.project_names()
        reports = [admin.scrub(name, None if args.json else stderr_line) for name in names]
        scanned = Text(*(line for report in reports for line in scrub_lines(report)))
        if not args.purge:
            count = f"Count only, nothing was deleted. --purge deletes the {len(_old(reports))} old object(s)."
            return Result(scrub_json(False, reports), Views(scanned, Text(count)))
        if not args.json:
            scanned.write(ctx.out)  # what was found comes before the question
        if _old(reports) and not self.confirmed(ctx, reports):
            return Result(None, Text("Nothing was deleted."), 1)
        parts: list[View] = []
        for report in reports:
            if any(found["state"] == "old" for found in report["found"]):
                admin.purge(report)
                parts.append(purged_view(report))
        if not _old(reports):
            parts.append(Text("Nothing to delete."))
        return Result(scrub_json(True, reports), Views(*parts))

    @staticmethod
    def confirmed(ctx: HubContext, reports: list[dict]) -> bool:
        old = _old(reports)
        graphs = sum(1 for found in old if found["kind"] == "kg-graph")
        size = _bytes(sum(found["size"] for found in old))
        about = (
            []
            if ctx.args.json
            else [
                f"About to delete {len(old)} old object(s) of the hub, {graphs} graph(s) and {len(old) - graphs} "
                f"source blob(s), {size}. This cannot be undone."
            ]
        )
        if ctx.args.yes:
            for line in about:
                print(line, file=ctx.out)
            return True
        return confirmed(ctx, about, "Delete them? [y/N] ")


KG_PUSH = KgPush()
KG_BUILDS = KgBuilds()
KG_BUILD = KgBuild()
KG_HEALTH = KgHealth()
KG_FORGET_SOURCE = KgForgetSource()
KG_PRUNE = KgPrune()
KG_SCRUB = KgScrub()

KG = Group(
    "kg",
    "knowledge graphs on the hub: push runs, list and queue builds, their health, clusters, prune old graphs",
    (
        KG_PUSH,
        KG_BUILDS,
        KG_BUILD,
        KG_HEALTH,
        KG_FORGET_SOURCE,
        KG_PRUNE,
        KG_SCRUB,
        *clusters.NODES,
        *review.NODES,
        *decisions.NODES,
        *eval_set.NODES,
        *optimize.NODES,
        init.KG_INIT,
        *profile.NODES,
        *semantic.NODES,
        *settings.NODES,
        upload.KG_UPLOAD,
        upload.KG_PIPELINE,
        upload.KG_UPLOADS,
        *entities.NODES,
        answer.KG_ANSWER,
        *entity_map.NODES,
        *graphs.NODES,
        *branches.NODES,
    ),
)
