"""``evo-agents kg sync``: run the connectors of a project's sources and push each run to the hub, with no corpus here
(``evo_agents.hub.kg_hub_sync``); ``--push`` is what every sync does now.

``--due`` syncs only the sources whose refresh interval has passed, and ``--due --all``, the LaunchAgent's command
(``kg schedule``), every project the hub takes a run of from this machine. The hub prunes and scrubs what it holds
(``kg prune``, ``kg scrub``); the machine keeps nothing to prune.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Result, Text, UsageError
from evo_agents.kg.cli.base import WITH_PROJECT, KgCommand, KgContext, project_failed, shown
from evo_agents.kg.project import ProjectError


def run_lines(results) -> list[str]:
    """What a person reads of the runs of a sync, one source after the other."""
    lines = []
    for r in results:
        mark = "OK  " if r.ok else "FAIL"
        if r.unchanged:
            lines.append(
                f"{mark} {r.source:<20} unchanged: the commit and config it read last, nothing read, {r.seconds}s"
            )
        else:
            lines.append(
                f"{mark} {r.source:<20} {r.items} item(s), {r.tombstones} tombstone(s), {r.removals} removed, "
                f"{r.errors} error(s), {r.rejected} rejected, {r.seconds}s"
            )
        if r.redactions:
            redacted = sum(sum(kinds.values()) for kinds in r.redactions.values())
            lines.append(f"     redacted: {redacted} secret(s) in {len(r.redactions)} item(s), counted in audit.jsonl")
        for h in r.held:
            lines.append(f"     held: {h['count']} removal(s) in {h['scope']}: {h['reason']}")
            lines.append("           review, then run: evo-agents kg sync --accept-removals --source " + r.source)
        lines += [f"     {issue}" for issue in r.issues[:8]]
        lines += [f"     warning: {warning}" for warning in r.warnings[:8]]
        if r.exception:
            lines.append(f"     connector raised {r.exception}")
        lines += [f"     stderr: {line}" for line in r.stderr_tail[-5:]]
    return lines


def due_lines(outcome) -> list[str]:
    """What a person reads of the sync of one project's due sources."""
    # Everything goes to stdout: under launchd both streams share schedule.log, and stdout is buffered.
    if outcome.skipped:
        return [f"project {outcome.project}: skipped, {outcome.skipped}"]
    if outcome.error:
        return [f"FAIL project {outcome.project}: {outcome.error}"]
    if not outcome.due:
        return [f"project {outcome.project}: nothing due"]
    lines = [f"project {outcome.project}: {len(outcome.due)} source(s) due", *run_lines(outcome.runs)]
    if outcome.build is not None:
        lines.append(outcome.build.summary_line())
        lines += [f"warning: {warning}" for warning in outcome.build.warnings]
    return lines


def syncer(project):
    """The sync of ``project`` against the hub this machine is signed in to (``evo_agents.hub.kg_hub_sync``)."""
    from evo_agents.hub.kg_hub_sync import hub_sync

    return hub_sync(project, plans=hub_plans())


def hub_plans():
    """What the harness connector reads the plans of the hub through, in a harness whose harness.yaml names a hub
    project (``ConnectorContext.plans``); on a machine not signed in to a hub it reads the copies in plans/."""
    from evo_agents.hub.kg_plans import reader

    return reader()


class KgSync(KgCommand):
    name = "sync"
    help = "run connectors and push their runs to the hub"
    hub_errors = True
    arguments = (
        *WITH_PROJECT,
        Arg("--source", action="append", help="only this source (repeatable)"),
        Arg("--accept-removals", action="store_true", help="apply deletions a guard held back"),
        Arg(
            "--build",
            action="store_true",
            help="then ask the hub for a build of the graph and wait for it (with --due: only if a source ran)",
        ),
        Arg(
            "--due", action="store_true", help="only sources whose refresh interval has passed since their last ok run"
        ),
        Arg("--all", action="store_true", help="with --due: every project in projects.json whose harness still exists"),
        Arg("--push", action="store_true", help="kept for older schedules: every sync pushes its runs to the hub"),
    )

    def check(self, args: argparse.Namespace) -> None:
        if args.all and not args.due:
            raise UsageError("--all needs --due; a full sync of every project is not offered")
        if args.due and args.source:
            raise UsageError("--due picks the sources itself; drop --source")
        if args.due and args.all and (args.project or args.accept_removals):
            raise UsageError("--all goes with --due, --build, --push and --json only")

    def run(self, ctx: KgContext) -> Result:
        if ctx.args.due:
            return self.due_all(ctx) if ctx.args.all else self.due(ctx)
        from evo_agents.hub.kg_hub_sync import cleared_sources, sync_project

        args, project = ctx.args, ctx.project
        results, build = sync_project(
            syncer(project), args.source or None, accept=args.accept_removals, build=args.build
        )
        ok = all(r.ok and not r.held for r in results) and (build is None or build.ok)
        left = [] if args.source else cleared_sources(project)[1]
        body = {"project": project.name, "runs": [r.to_json() for r in results], "left_out": left}
        if build is not None:
            body["build"] = build.to_json()
        lines = run_lines(results)
        lines += [
            f"SKIP {source_id}: the hub sink does not clear its label, so the hub takes none of its runs"
            for source_id in left
        ]
        if build is not None:
            lines.append(build.summary_line())
        return shown(ctx, body, Text(*lines), 0 if ok else 1)

    def due(self, ctx: KgContext) -> Result:
        from evo_agents.hub.kg_hub_sync import sync_due

        project = ctx.project
        try:
            outcome = sync_due(syncer(project), build=ctx.args.build, accept=ctx.args.accept_removals)
        except ProjectError as exc:
            raise project_failed(exc) from exc
        return shown(ctx, outcome.to_json(), Text(*due_lines(outcome)), 0 if outcome.ok else 1)

    def due_all(self, ctx: KgContext) -> Result:
        """``kg sync --due --all``: sync every project the hub takes a run of from this machine."""
        from evo_agents.hub.client import Hub, HubError, load_credentials
        from evo_agents.hub.kg_hub_sync import sync_all_due
        from evo_agents.hub.kg_remote import local_projects
        from evo_agents.kg.ids import utc_now

        failed = None
        try:
            credentials = load_credentials()
            hub = Hub(credentials.url, credentials.token)
            outcomes = sync_all_due(hub, local_projects(None, True), build=ctx.args.build, plans=hub_plans())
        except HubError as exc:
            outcomes, failed = [], f"the hub: {exc}"
        ok = failed is None and all(o.ok for o in outcomes)
        body = {"ok": ok, "projects": [o.to_json() for o in outcomes]}
        if failed is not None:
            body["error"] = failed
        lines = [f"kg sync --due --all at {utc_now()}: {len(outcomes)} project(s)"]
        if failed is not None:
            lines.append(f"FAIL sync: {failed}")
        for outcome in outcomes:
            lines += due_lines(outcome)
        return shown(ctx, body, Text(*lines), 0 if ok else 1)


SYNC = KgSync()
