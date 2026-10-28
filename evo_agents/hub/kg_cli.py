"""``evo-agents hub kg push|builds|build|health|forget-source|prune|scrub|clusters``, and the hub backend of ``kg serve`` and
``kg query``.

``push`` sends the runs a project's corpus holds and the hub lacks (``evo_agents.hub.kg_push``): ``--project P`` one
project of this machine, ``--all`` every project of projects.json the hub takes a push for. It reports its progress on
stderr, a line per run and per batch of blobs, and its outcome on stdout. ``builds`` lists a project's builds and the
jobs still queued; ``build`` (the writer role) queues one and with ``--wait`` waits until it has finished, exiting 1
when it failed. ``health`` prints the health of a project's graph over time as the hub measures it
(``evo_agents.hub.server.kg_health``): for each git source of the latest measure the commit read, how many commits of
the default branch it lacks and for how long, the files missing, stale and deleted, then the retrieval evaluation and
the builds that changed nothing. ``forget-source`` (the writer role) takes every run of one or more sources off the
hub and queues a build without them (``evo_agents.hub.server.kg``), which a source must go through before
knowledge.yaml drops it; at a terminal it asks first unless ``--yes``. ``prune`` (a hub admin) runs the retention of built graphs now (``evo_agents.hub.kg_prune``): the
artifacts of graphs older than each project's ``--keep`` newest leave the bucket, and ``--dry-run`` only says which.
``scrub`` (a hub admin) reads every built graph and source blob of a project, or of every project, page by page
(``evo_agents.hub.server.kg_scrub``) and lists those holding a secret by hash, kind and the kinds of secret, never a
value; ``--purge`` says what it is about to delete, asks (or takes ``--yes``), and has the hub delete the old ones.
``clusters`` lists the clusters of a project's graph the caller can see, and ``clusters show ID`` one of them
(``evo_agents.hub.kg_clusters_cli``). ``review`` lists the review queue the caller can see, and ``review dismiss FP``
dismisses one item of it (``evo_agents.hub.kg_review_cli``). ``decisions`` lists the decisions on the graph the caller
can see, and a writer takes or steps one (``evo_agents.hub.kg_decisions_cli``).

``open_session`` picks where kg_* are answered. ``local`` reads the store on this machine, as before the hub.
``hub`` sends every call to the hub's /mcp as a tools/call (``RemoteSession``), the seven tools with the same names and
results, so an MCP client sees no difference. ``auto`` takes the hub when this machine is signed in and the hub has
a successful build of the project it can show the caller, and the local store otherwise, including when the hub does
not answer within PROBE_TIMEOUT. Inside a run of this machine's worker (``mcp_proxy.run_credentials``), ``auto`` and
``hub`` ask the hub as the agent of that run, with the worker's token and X-Evo-Run, as ``evo-agents hub mcp`` does
there: the hub binds the session to the run's project, so a run whose worktree names no project of this machine, or
whose machine is not signed in, still reads its graph. Standard library only.
"""

from __future__ import annotations

import sys
import time
from urllib.parse import quote

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in, _table, _when
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.kg_push import push_all, push_project

BACKENDS = ("auto", "local", "hub")
PROBE_TIMEOUT = 5.0  # seconds auto waits for the hub before answering locally
WAIT_INTERVAL = 2.0
DEFAULT_WAIT = 1800.0
FINISHED = ("succeeded", "failed")
PRUNE_TIMEOUT = 600.0  # seconds a prune may take: it deletes objects in the blob store
SCRUB_TIMEOUT = 600.0  # seconds one page of a scrub, or one purge, may take: it reads objects in the blob store
PURGE_BATCH = 500  # hashes in one purge request; the hub takes 1000
SCRUB_SHOWN = 20  # objects listed per project in text; --json lists them all


def _base(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}"


# Pushing


def local_projects(project: str | None, everything: bool) -> list[tuple[str, object]]:
    """(name, project or the error it failed to load with) for ``--project`` or ``--all``."""
    from evo_agents.kg.project import ProjectError, load_project_at, resolve_project
    from evo_agents.kg.sync import indexed_projects

    if everything:
        found = []
        for name, root in indexed_projects():
            try:
                found.append((name, load_project_at(root)))
            except Exception as exc:  # a broken harness is reported on its project, the others go on
                found.append((name, exc if isinstance(exc, ProjectError) else ProjectError(f"{type(exc).__name__}")))
        return found
    loaded = resolve_project(project)
    return [(loaded.name, loaded)]


def push(hub: Hub, project: str | None, everything: bool, progress=None) -> list:
    """The push reports of ``--project`` or ``--all``; ``progress`` receives a line per run and per batch of blobs.
    Raises ProjectError when the one project does not load."""
    projects = local_projects(project, everything)
    if everything:
        return push_all(hub, projects, progress)
    return [push_project(hub, loaded, progress) for _, loaded in projects]


def _progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)  # stderr: stdout keeps the summary, or the JSON of --json


def push_json(reports: list) -> dict:
    """What ``hub kg push --json`` prints."""
    return {"ok": all(r.ok for r in reports), "projects": [r.to_json() for r in reports]}


def print_reports(reports: list, out=None) -> None:
    out = out or sys.stdout
    for report in reports:
        print(report.summary_line(), file=out)
        for error in report.errors:
            print(f"  error: {error}", file=out)


@_client_command
def cmd_push(args) -> int:
    from evo_agents.kg.project import ProjectError

    if args.all == bool(args.project):
        raise HubError("pass --project P or --all")
    hub, _ = _signed_in()
    try:
        reports = push(hub, args.project, args.all, _progress)
    except ProjectError as exc:
        raise HubError(str(exc)) from None
    if args.json:
        _print_json(push_json(reports))
    else:
        print_reports(reports)
    return 0 if all(r.ok for r in reports) else 1


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
        _when(build.get("queued_at")),
        _when(build.get("finished_at")),
        build.get("runs") if build.get("runs") is not None else "-",
        (build.get("content_hash") or "-")[:19],
        outcome[:80],
    )


@_client_command
def cmd_builds(args) -> int:
    hub, _ = _signed_in()
    found = hub.call("GET", f"{_base(args.project)}/builds?limit={args.limit}")
    if args.json:
        _print_json(found)
        return 0
    rows = [_build_row(build) for build in found["builds"]]
    if rows:
        _table(("BUILD", "STATUS", "QUEUED (UTC)", "FINISHED (UTC)", "RUNS", "CONTENT HASH", "OUTCOME"), rows)
    else:
        print(f"project {args.project} has no build on the hub yet")
    for job in found["jobs"]:
        state = "running" if job["status"] == "doing" else "waiting"
        print(f"job {job['job_id']} {state}" + (f" (build {job['build_id']})" if job["build_id"] else ""))
    return 0


def wait_for(hub: Hub, project: str, build_id: int, timeout: float, sleep=time.sleep, clock=time.monotonic) -> dict:
    deadline = clock() + timeout
    while True:
        build = hub.call("GET", f"{_base(project)}/builds/{build_id}")
        if build["status"] in FINISHED:
            return build
        if clock() >= deadline:
            raise HubError(f"build {build_id} of project {project} has not finished after {timeout:g}s")
        sleep(WAIT_INTERVAL)


@_client_command
def cmd_build(args) -> int:
    hub, _ = _signed_in()
    queued = hub.call("POST", f"{_base(args.project)}/builds")
    build = queued.get("build")
    if build is None:
        raise HubError(f"a build of project {args.project} is waiting already, but the hub names none")
    if args.wait:
        build = wait_for(hub, args.project, build["id"], args.timeout)
    if args.json:
        _print_json({"queued": queued["queued"], "build": build})
    else:
        verb = "Queued" if queued["queued"] else "A build was waiting already:"
        print(f"{verb} build {build['id']} of project {args.project}: {build['status']}.")
        if build["status"] == "succeeded":
            print(f"{_counts(build)}, content {build['content_hash']}{_artifact_note(build)}")
        elif build["status"] == "failed":
            print(f"error: {build['error']}", file=sys.stderr)
    return 1 if build["status"] == "failed" else 0


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


@_client_command
def cmd_health(args) -> int:
    hub, _ = _signed_in()
    found = hub.call("GET", f"/v1/projects/{quote(args.project, safe='')}/curator/kg-health?limit={args.limit}")
    if args.json:
        _print_json(found)
        return 0
    measures = found["measures"]
    if not measures:
        print(f"the hub has not measured the graph of project {args.project} yet: curator.collect does, every 6 hours")
        return 0
    latest = measures[0]
    print(f"Measured {_when(latest['measured_at'])} (UTC), build {latest.get('build_id') or '-'}:")
    if latest.get("error"):
        print(f"error: {latest['error']}", file=sys.stderr)
    rows = [_source_row(source) for source in latest["sources"]]
    if rows:
        _table(("SOURCE", "COMMIT", "BEHIND", "LAG (H)", "MISSING", "STALE", "DELETED", "WHY"), rows)
    print(_eval_line(latest.get("eval")))
    builds = found["builds"]
    unchanged = sum(1 for build in builds if not build["changed"])
    print(f"{len(builds)} successful builds listed, {unchanged} of them changed nothing")
    return 0


def _bytes(size: int) -> str:
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def forget(hub: Hub, project: str, sources: list[str]) -> list[dict]:
    """DELETE /v1/kg/{project}/sources/{source} for each of ``sources``, in order: what the hub answered."""
    return [hub.call("DELETE", f"{_base(project)}/sources/{quote(source, safe='')}") for source in sources]


def forget_json(project: str, found: list[dict]) -> dict:
    """What ``hub kg forget-source --json`` prints."""
    return {"project": project, "sources": found}


def forget_line(project: str, found: dict) -> str:
    runs = found["ingested"] + found["pending"]
    if not runs:
        return f"the hub holds no run of source {found['source']} of project {project}; nothing changed"
    line = f"took {runs} run(s) of source {found['source']} off project {project}"
    if found["logs"]:
        line += f"; {found['logs']} log(s) leave the blob store with the next prune"
    build = found.get("build")
    if build:
        line += f"; build {build['id']} {build['status']}"
    return line


@_client_command
def cmd_forget_source(args) -> int:
    hub, _ = _signed_in()
    sources = list(dict.fromkeys(args.source))
    if not args.yes and not args.json and sys.stdin.isatty():
        print(
            f"About to take every run of {', '.join(sources)} off project {args.project} on the hub; the graph is built "
            "again without them."
        )
        if input("Go on? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
    found = forget(hub, args.project, sources)
    if args.json:
        _print_json(forget_json(args.project, found))
        return 0
    for one in found:
        print(forget_line(args.project, one))
    still = [one["source"] for one in found if one["declared"] and one["ingested"] + one["pending"]]
    if still:
        print(
            f"warning: the knowledge config on the hub still declares {', '.join(still)}: remove it from "
            "knowledge.yaml and push (`evo-agents kg sync --push`), or a machine holding its runs sends them again",
            file=sys.stderr,
        )
    return 0


@_client_command
def cmd_prune(args) -> int:
    hub, _ = _signed_in()
    body = {"project": args.project, "keep": args.keep, "dry_run": args.dry_run}
    report = hub.call("POST", "/v1/admin/kg/prune", body, timeout=PRUNE_TIMEOUT)
    if args.json:
        _print_json(report)
        return 0
    rows = [
        (p["project"], p["artifacts"], p["kept"], p["pruned"], _bytes(p["pruned_bytes"]), p["builds"])
        for p in report["projects"]
    ]
    if rows:
        _table(("PROJECT", "ARTIFACTS", "KEPT", "PRUNED", "PRUNED SIZE", "BUILDS"), rows)
    else:
        print("no project has a built graph on the hub")
    verb = "would delete" if report["dry_run"] else "deleted"
    print(
        f"{'Dry run, keeping' if report['dry_run'] else 'Kept'} the artifacts of the {report['keep']} newest graph(s) "
        f"of each project: {verb} {report['deleted']} object(s), {_bytes(report['deleted_bytes'])}."
    )
    if report["pending"]:
        print(f"{report['pending']} object(s) still wait to be deleted; the next prune tries again.", file=sys.stderr)
    return 0


# Old copies of secrets


def scrub_scan(hub: Hub, project: str, progress=None) -> dict:
    """Every page of POST /v1/admin/kg/scrub for ``project``, added up; ``purged`` is None until a purge."""
    report = {"project": project, "scanned": 0, "scanned_bytes": 0, "missing": 0, "found": [], "purged": None}
    after = None
    while True:
        body = {"project": project, **({"after": after} if after else {})}
        page = hub.call("POST", "/v1/admin/kg/scrub", body, timeout=SCRUB_TIMEOUT)
        for key in ("scanned", "scanned_bytes", "missing"):
            report[key] += page[key]
        report["found"] += page["found"]
        if progress:
            progress(f"{project}: {report['scanned']} object(s) read, {len(report['found'])} hold a secret")
        after = page["next"]
        if not after:
            return report


def scrub_purge(hub: Hub, report: dict) -> None:
    """Ask the hub to delete the old objects ``report`` found, PURGE_BATCH at a time; what it answered goes into
    ``report["purged"]``."""
    old = [found["sha256"] for found in report["found"] if found["state"] == "old"]
    purged = {"purged": [], "kept": [], "builds": 0, "deleted": 0, "deleted_bytes": 0, "pending": 0}
    for start in range(0, len(old), PURGE_BATCH):
        body = {"project": report["project"], "sha256": old[start : start + PURGE_BATCH]}
        answer = hub.call("POST", "/v1/admin/kg/scrub/purge", body, timeout=SCRUB_TIMEOUT)
        for key in ("purged", "kept"):
            purged[key] += answer[key]
        for key in ("builds", "deleted", "deleted_bytes"):
            purged[key] += answer[key]
        purged["pending"] = answer["pending"]
    report["purged"] = purged


def scrub_json(purge: bool, reports: list[dict]) -> dict:
    """What ``hub kg scrub --json`` prints."""
    return {"purge": purge, "projects": reports}


def _old(reports: list[dict]) -> list[dict]:
    return [found for report in reports for found in report["found"] if found["state"] == "old"]


def _print_scrub(report: dict) -> None:
    found = report["found"]
    states = {state: sum(1 for f in found if f["state"] == state) for state in ("old", "live", "newest")}
    print(
        f"{report['project']}: {len(found)} of {report['scanned']} object(s) hold a secret: {states['old']} old, "
        f"{states['live']} live source blob(s), {states['newest']} newest graph(s)"
        + (f"; {report['missing']} missing from the bucket" if report["missing"] else "")
    )
    for item in found[:SCRUB_SHOWN]:
        kinds = ", ".join(f"{kind} {count}" for kind, count in item["secrets"].items())
        print(f"  {item['state']:<6}  {item['kind']:<8}  {item['sha256'][:16]}  {_bytes(item['size'])}  {kinds}")
    if len(found) > SCRUB_SHOWN:
        print(f"  and {len(found) - SCRUB_SHOWN} more: --json lists them all")


def _print_purged(report: dict) -> None:
    purged = report["purged"]
    reasons = {}
    for kept in purged["kept"]:
        reasons[kept["reason"]] = reasons.get(kept["reason"], 0) + 1
    kept = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
    print(
        f"{report['project']}: deleted {len(purged['purged'])} object(s), {_bytes(purged['deleted_bytes'])} left the "
        f"bucket; {purged['builds']} build(s) lost their graph" + (f"; kept {kept}" if kept else "")
    )
    if purged["pending"]:
        print(f"{purged['pending']} object(s) still wait to be deleted; the next prune tries again.", file=sys.stderr)


def _scrub_confirmed(args, reports: list[dict]) -> bool:
    old = _old(reports)
    graphs = sum(1 for found in old if found["kind"] == "kg-graph")
    size = _bytes(sum(found["size"] for found in old))
    if not args.json:
        print(
            f"About to delete {len(old)} old object(s) of the hub, {graphs} graph(s) and {len(old) - graphs} source "
            f"blob(s), {size}. This cannot be undone."
        )
    return args.yes or input("Delete them? [y/N] ").strip().lower() in ("y", "yes")


@_client_command
def cmd_scrub(args) -> int:
    if args.purge and not args.yes and (args.json or not sys.stdin.isatty()):
        print("error: --purge deletes for good; pass --yes too when no terminal can confirm it", file=sys.stderr)
        return 2
    hub, _ = _signed_in()
    names = [args.project] if args.project else [p["name"] for p in hub.call("GET", "/v1/projects")]
    reports = [scrub_scan(hub, name, None if args.json else _progress) for name in names]
    if not args.json:
        for report in reports:
            _print_scrub(report)
    if not args.purge:
        if args.json:
            _print_json(scrub_json(False, reports))
        else:
            print(f"Count only, nothing was deleted. --purge deletes the {len(_old(reports))} old object(s).")
        return 0
    if _old(reports) and not _scrub_confirmed(args, reports):
        print("Nothing was deleted.")
        return 1
    for report in reports:
        if any(found["state"] == "old" for found in report["found"]):
            scrub_purge(hub, report)
            if not args.json:
                _print_purged(report)
    if args.json:
        _print_json(scrub_json(True, reports))
    elif not _old(reports):
        print("Nothing to delete.")
    return 0


def register_kg(hsub) -> None:
    from evo_agents.hub.cli import _positive

    kg = hsub.add_parser(
        "kg",
        help="knowledge graphs on the hub: push runs, list and queue builds, their health, clusters, prune old graphs",
    )
    ksub = kg.add_subparsers(dest="kg_command", required=True)

    push_parser = ksub.add_parser("push", help="send the runs of a project's corpus that the hub does not have")
    push_parser.add_argument("--project", help="project name or harness path")
    push_parser.add_argument(
        "--all", action="store_true", help="every project in projects.json the hub takes a push for from you"
    )
    json_option(push_parser, returns_object("ok", "projects"))
    push_parser.set_defaults(func=cmd_push)

    builds = ksub.add_parser("builds", help="a project's builds on the hub, newest first, and the queued jobs")
    builds.add_argument("--project", required=True, help="project name on the hub")
    builds.add_argument("--limit", type=int, default=20, help="builds to show (default: 20, at most 100)")
    json_option(builds, returns_object("builds", "jobs", schema="Builds"))
    builds.set_defaults(func=cmd_builds)

    build = ksub.add_parser("build", help="queue a build of a project's graph (needs the writer role)")
    build.add_argument("--project", required=True, help="project name on the hub")
    build.add_argument("--wait", action="store_true", help="wait until the build has finished; exit 1 if it failed")
    build.add_argument(
        "--timeout", type=float, default=DEFAULT_WAIT, help="seconds --wait waits at most (default: 1800)"
    )
    json_option(build, returns_object("queued", "build", schema="Queued"))
    build.set_defaults(func=cmd_build)

    health = ksub.add_parser("health", help="the health of a project's graph over time, as the hub measures it")
    health.add_argument("--project", required=True, help="project name on the hub")
    health.add_argument("--limit", type=int, default=30, help="measures and builds to show (default: 30, at most 200)")
    json_option(health, returns_object("project", "measures", "builds", schema="KgHealthView"))
    health.set_defaults(func=cmd_health)

    forget_parser = ksub.add_parser(
        "forget-source",
        help="take every run of a source off the hub, so knowledge.yaml can drop it (needs the writer role)",
    )
    forget_parser.add_argument("--project", required=True, help="project name on the hub")
    forget_parser.add_argument(
        "--source", required=True, action="append", help="the source's id in knowledge.yaml; repeat for several"
    )
    forget_parser.add_argument("--yes", action="store_true", help="do not ask first at a terminal")
    json_option(forget_parser, returns_object("project", "sources"))
    forget_parser.set_defaults(func=cmd_forget_source)

    prune = ksub.add_parser(
        "prune", help="delete the artifacts of graphs older than each project's newest few (needs a hub admin)"
    )
    prune.add_argument("--project", help="project name on the hub (default: every project)")
    prune.add_argument(
        "--keep",
        type=_positive,
        help="newest graphs of each project whose artifact stays (default: the hub's EVO_HUB_KG_KEEP_ARTIFACTS, 3)",
    )
    prune.add_argument("--dry-run", action="store_true", help="say what would be deleted, and delete nothing")
    json_option(
        prune,
        returns_object("dry_run", "keep", "projects", "deleted", "deleted_bytes", "pending", schema="KgPruned"),
    )
    prune.set_defaults(func=cmd_prune)

    scrub = ksub.add_parser(
        "scrub", help="find old graphs and source blobs holding a secret, and with --purge delete them (a hub admin)"
    )
    scrub.add_argument("--project", help="project name on the hub (default: every project)")
    scrub.add_argument("--purge", action="store_true", help="delete the old ones; newest graphs and live blobs stay")
    scrub.add_argument("--yes", action="store_true", help="with --purge: delete without asking")
    json_option(scrub, returns_object("purge", "projects"))
    scrub.set_defaults(func=cmd_scrub)

    from evo_agents.hub import kg_clusters_cli

    kg_clusters_cli.register(ksub)

    from evo_agents.hub import kg_review_cli

    kg_review_cli.register(ksub)

    from evo_agents.hub import kg_decisions_cli

    kg_decisions_cli.register(ksub)

    from evo_agents.hub import kg_eval_set_cli

    kg_eval_set_cli.register(ksub)

    from evo_agents.hub import kg_optimize_cli

    kg_optimize_cli.register(ksub)


# The hub backend of kg serve and kg query


class RemoteSession:
    """``Session.call`` answered by the hub: a tools/call to its /mcp for ``project`` through ``sink``
    (``evo_agents.hub.mcp_proxy.McpClient``) that asks for the structured result too, as a local Session gives it. A
    hub that cannot be reached or refuses the call gives a tool error naming the hub."""

    def __init__(self, hub: Hub, project: str | None, sink: str, run: int | None = None):
        self.hub = hub
        self.project = project
        self.sink = sink
        self.run = run  # the run whose agent asks, with a worker's token: the hub binds the session to its project

    def call(self, name: str, args: dict) -> dict:
        from evo_agents.hub.mcp_proxy import McpClient
        from evo_agents.kg.serve import DATA_META, TOOLS, _error

        if name not in {tool["name"] for tool in TOOLS}:
            return _error(f"unknown tool {name!r}")
        try:
            client = McpClient(self.hub.url, self.hub.token, self.project, self.sink, run=self.run)
            return client.call_tool(name, args or {}, {DATA_META: True})
        except HubError as exc:
            return _error(f"{exc} (hub {self.hub.url})")


def hub_has_graph(hub: Hub, project: str) -> bool:
    """Whether the hub has a successful build of ``project`` it shows the caller."""
    found = hub.call("GET", f"{_base(project)}/builds?status=succeeded&limit=1")
    return bool(found.get("builds"))


def open_session(backend: str, project: str | None, sink: str):
    """What answers kg_* for ``kg serve`` and ``kg query``: a local Session or a RemoteSession (see the module)."""
    from evo_agents.kg.project import ProjectError, resolve_project
    from evo_agents.kg.serve import Session, make_session

    if backend == "local":
        return make_session(project, sink)
    from evo_agents.hub.mcp_proxy import run_credentials, session_credentials

    run = run_credentials()
    if run is not None:  # the hub knows the run's project; a worker token reads no build list to probe
        return RemoteSession(Hub(run.url, run.token), project, sink, run=run.run)
    try:
        name = resolve_project(project).name
    except ProjectError as exc:
        name = project if project and "/" not in project and project not in (".", "..") else None
        if name is None:
            return make_session(project, sink) if backend == "auto" else Session(None, sink, error=str(exc))
    try:
        credentials = session_credentials()  # never a run's token of the hub, which /mcp does not take
        hub = Hub(credentials.url, credentials.token)
        if backend == "auto" and not hub_has_graph(Hub(hub.url, hub.token, timeout=PROBE_TIMEOUT), name):
            return make_session(project, sink)
    except HubError as exc:
        return make_session(project, sink) if backend == "auto" else Session(None, sink, error=str(exc))
    return RemoteSession(hub, name, sink)
