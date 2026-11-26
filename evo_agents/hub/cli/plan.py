"""``evo-agents hub plan``: plans on the hub and their read-only copies in a harness.

``import`` pushes every plans/<area>/*.yaml of a harness once (a plan already on the hub with the same digest is
left alone, one that differs is reported, never overwritten). ``put`` creates a plan from a draft, or replaces one
with ``--if-revision``, then rewrites the file as the hub's copy. ``patch``, ``step`` and ``complete`` change one
plan on the hub, and ``close-finished`` completes every active plan whose steps are all done or descoped (the hub
completes such a plan on the write that closes its last step; this closes those written before it did); ``export``
writes the copies back into the harness (``evo_agents.hub.mirror``). ``list``,
``show`` and ``history`` read, and ``handoff`` prints what a new session needs to take a plan up, as the hub builds
it within a token budget (``evo_agents.hub.handoff``), the same text as the MCP tool plan_handoff.

The writes themselves, and their retry on a revision conflict, are in ``evo_agents.hub.plan_writes``; ``put`` never
retries, so a replaced plan has to be read again by the person who wrote it.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness
around the current directory. Standard library and PyYAML only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Commands): :class:`Plans` is the
gateway to the plan routes the commands read, :class:`ItemChange` the value object that turns the options of
``patch`` and ``step`` into the keys of one item to set.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from evo_agents.clikit import Arg, Group, Result, Table, Text, Verbatim, returns_array, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.client import HubError
from evo_agents.hub.handoff import DEFAULT_BUDGET_TOKENS, MAX_BUDGET_TOKENS, MIN_BUDGET_TOKENS, routing_line
from evo_agents.hub.mirror import export, render
from evo_agents.hub.plan_writes import (
    PLAN_KEYS,
    WRITTEN,
    abandon_plan,
    checked_plan_id,
    complete_plan,
    import_plans,
    patch_item,
    put_file,
    reopen_plan,
)
from evo_agents.hub.plans import AREAS, SECTIONS, STEP_STATUSES, UPDATABLE

SHOWN_WARNINGS = 5

REVISION_KEYS = ("revision", "area", "digest", "summary", "actor", "created_at")
SUMMARY_KEYS = (
    "plan_id",
    "area",
    "revision",
    "digest",
    "title",
    "steps_total",
    "steps_done",
    "updated_at",
    "updated_by",
    "problems",
    "trace",
    "goal",
    "steps",
    "current_step",
    "repos",
)
CLOSED_KEYS = ("plan_id", "title", "steps_total", "revision", "closed", "error")
HANDOFF_KEYS = (
    "project",
    "plan_id",
    "revision",
    "step",
    "budget_tokens",
    "tokens",
    "cut",
    "text",
    "difficulty",
    "recommended_model",
    "routing_reason",
)


class Plans(ProjectResource):
    """The plans of a project the commands read: GET /v1/projects/{p}/plans and the routes below it. The writes go
    through ``evo_agents.hub.plan_writes``, which retries them on a revision conflict."""

    collection = "plans"

    def list(self, area: str | None = None) -> list[dict]:
        return self.call("GET", area=area)

    def get(self, plan_id: str, revision: int | None = None) -> dict:
        if revision:
            return self.call("GET", checked_plan_id(plan_id), "revisions", revision)
        return self.call("GET", checked_plan_id(plan_id))

    def revisions(self, plan_id: str) -> list[dict]:
        return self.call("GET", checked_plan_id(plan_id), "revisions")

    def handoff(self, plan_id: str, budget_tokens: int, step: str | None = None) -> dict:
        return self.call("GET", checked_plan_id(plan_id), "handoff", budget_tokens=budget_tokens, step=step)


def warning_lines(result: dict) -> list[str]:
    """The first warnings the hub gave with a plan it took, and how many more there are."""
    warnings = result.get("warnings") or []
    lines = [f"warning: {warning['path'] or '<root>'}: {warning['message']}" for warning in warnings[:SHOWN_WARNINGS]]
    if len(warnings) > SHOWN_WARNINGS:
        lines.append(
            f"... {len(warnings) - SHOWN_WARNINGS} more warning(s); `evo-agents harness validate -v` lists them"
        )
    return lines


def written_lines(result: dict, what: str) -> list[str]:
    """What a write to one plan did: ``what`` it at a new revision, or nothing."""
    plan = f"plan {result['plan_id']}"
    if result["changed"]:
        said = f"{what} {plan}: revision {result['revision']}. `evo-agents hub plan export` updates its copy."
    else:
        said = f"{plan} already holds that (revision {result['revision']}); nothing changed."
    return [said, *warning_lines(result)]


PLAN_REF = Arg("plan", metavar="PLAN")
IF_REVISION = Arg("--if-revision", type=int)
IF_REVISION_OR_RETRY = Arg(
    "--if-revision", type=int, help="fail on a conflict instead of retrying on the latest revision"
)
ROOT = Arg("root", metavar="HARNESS_ROOT", nargs="?", help="the harness (default: the cwd)")


# The harness


class PlanImport(HubCommand):
    name = "import"
    help = "push every plans/*/*.yaml of a harness the hub does not hold yet (same digest: left alone)"
    arguments = (ROOT, PROJECT)
    output = returns_object("root", "project", "plans")

    def run(self, ctx: HubContext) -> Result:
        result = import_plans(ctx.hub, ctx.args.root, ctx.args.project)
        return Result(result.as_json(), Text(*self.lines(result)), 1 if result.failed else 0)

    @staticmethod
    def lines(result) -> list[str]:
        lines = []
        for entry in result.plans:
            revision = f" r{entry['revision']}" if entry["revision"] else ""
            message = f": {entry['message']}" if entry["message"] else ""
            lines.append(f"{entry['status']:<9} {entry['path']}{revision}{message}")
        counts = {status: sum(1 for p in result.plans if p["status"] == status) for status in ("created", "unchanged")}
        lines.append(
            f"{len(result.plans)} file(s) for project {result.project}: {counts['created']} created, "
            f"{counts['unchanged']} already on the hub, {len(result.failed)} not pushed"
        )
        if not result.failed:
            lines.append(f"Next: `evo-agents hub plan export {result.root}` writes the copies the hub keeps.")
        return lines


class PlanExport(HubCommand):
    name = "export"
    help = "write the copy of every plan of the project into the harness, and commit them with --commit"
    arguments = (
        ROOT,
        Arg("--commit", action="store_true", help="commit the copies that changed, and nothing else in the checkout"),
        PROJECT,
    )
    output = returns_object("root", "project", "plans", "removed", "notes", "commit")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        result = export(ctx.hub, Path(args.root).expanduser() if args.root else None, args.project, commit=args.commit)
        return Result(result.as_json(), Text(*self.lines(result, args.commit)))

    @staticmethod
    def lines(result, commit: bool) -> list[str]:
        lines = [
            f"wrote    {entry['path']} (revision {entry['revision']})"
            for entry in result.plans
            if entry["status"] != "unchanged"
        ]
        lines += [f"removed  {path}" for path in result.removed]
        lines += [f"note: {note}" for note in result.notes]
        unchanged = sum(1 for p in result.plans if p["status"] == "unchanged")
        lines.append(
            f"{len(result.plans)} plan(s) of project {result.project} in {result.root}: "
            f"{len(result.plans) - unchanged} written, {unchanged} unchanged, {len(result.removed)} removed."
        )
        if commit:
            lines.append(
                f"Committed the copies as {result.commit[:12]}." if result.commit else "The copies match HEAD."
            )
        return lines


# Reading


class PlanList(HubCommand):
    name = "list"
    help = "the plans of a project you can see, with revision and progress"
    arguments = (Arg("--area", choices=AREAS), PROJECT)
    output = returns_array(*SUMMARY_KEYS, schema="PlanSummary")
    columns: ClassVar[tuple[str, ...]] = ("PLAN", "AREA", "REV", "STEPS DONE", "UPDATED (UTC)", "BY")

    def run(self, ctx: HubContext) -> Result:
        plans = Plans.of(ctx).list(ctx.args.area)
        return Result(plans, Table(self.columns, [self.row(plan) for plan in plans], bare=True))

    @staticmethod
    def row(plan: dict) -> tuple:
        return (
            plan["plan_id"],
            plan["area"],
            plan["revision"],
            f"{plan['steps_done']}/{plan['steps_total']}",
            when(plan["updated_at"]),
            plan["updated_by"],
        )


class PlanShow(HubCommand):
    name = "show"
    help = "a plan as its copy in git reads (with --json, as the hub answers)"
    arguments = (PLAN_REF, Arg("--revision", type=int, help="an earlier revision"), PROJECT)
    output = returns_object(
        *PLAN_KEYS,
        schema="Plan",
        variants=[("--revision", returns_object(*REVISION_KEYS, "label", "body", schema="RevisionBody"))],
    )

    def run(self, ctx: HubContext) -> Result:
        plan = Plans.of(ctx).get(ctx.args.plan, ctx.args.revision)
        return Result(plan, Verbatim(render(plan["body"], ctx.project, plan["revision"], plan["digest"])))


def by(revision: dict) -> str:
    """Who wrote a revision: the member, and the run that wrote it as them (an author run, or a local session's)."""
    run_id = revision.get("run_id")
    return revision["actor"] + (f" (run #{run_id})" if run_id else "")


class PlanHistory(HubCommand):
    name = "history"
    help = "the revisions of a plan: who changed it, when and what"
    arguments = (PLAN_REF, PROJECT)
    output = returns_array(*REVISION_KEYS, "run_id", schema="Revision")
    columns: ClassVar[tuple[str, ...]] = ("REV", "AREA", "WHEN (UTC)", "BY", "SUMMARY")

    def run(self, ctx: HubContext) -> Result:
        revisions = Plans.of(ctx).revisions(ctx.args.plan)
        rows = [(r["revision"], r["area"], when(r["created_at"]), by(r), r["summary"]) for r in revisions]
        return Result(revisions, Table(self.columns, rows, bare=True))


class PlanHandoff(HubCommand):
    name = "handoff"
    help = "what a new session needs to take a plan up: goal, steps done with decisions, steps left"
    arguments = (
        PLAN_REF,
        Arg("--step", metavar="KEY", help="for this step: the evidence of the steps it depends on first"),
        Arg(
            "--budget-tokens",
            type=int,
            default=DEFAULT_BUDGET_TOKENS,
            metavar="N",
            help=f"the most tokens the text may take, {MIN_BUDGET_TOKENS} to {MAX_BUDGET_TOKENS} "
            f"(default {DEFAULT_BUDGET_TOKENS})",
        ),
        PROJECT,
    )
    output = returns_object(*HANDOFF_KEYS, schema="PlanHandoff")

    def run(self, ctx: HubContext) -> Result:
        budget = ctx.args.budget_tokens
        if not MIN_BUDGET_TOKENS <= budget <= MAX_BUDGET_TOKENS:
            raise HubError(f"--budget-tokens is {MIN_BUDGET_TOKENS} to {MAX_BUDGET_TOKENS}, not {budget}")
        handoff = Plans.of(ctx).handoff(ctx.args.plan, budget, ctx.args.step)
        return Result(handoff, Verbatim(handoff["text"] + routing_line(handoff)))


# Writing


class PlanPut(HubCommand):
    name = "put"
    help = "create a plan from a file, or replace it with --if-revision; the file becomes the hub's copy"
    arguments = (
        Arg("file", metavar="FILE"),
        Arg("--if-revision", type=int, help="the revision on the hub this file replaces"),
        PROJECT,
    )
    output = WRITTEN

    def run(self, ctx: HubContext) -> Result:
        path = Path(ctx.args.file).expanduser()
        result = put_file(ctx.hub, path, ctx.args.project, ctx.args.if_revision)
        plan = f"plan {result['plan_id']} of project {result['project']}"
        if result["created"]:
            said = f"Created {plan} at revision 1; {path} is now its copy."
        elif result["changed"]:
            said = f"Replaced {plan}: revision {result['revision']}; {path} is now its copy."
        else:
            said = f"The hub already holds {path} as {plan}, revision {result['revision']}; {path} is now its copy."
        return Result(result, Text(said, *warning_lines(result)))


@dataclass(frozen=True)
class ItemChange:
    """The keys the options of ``patch`` or ``step`` set on one item of a plan (Value Object). Raises HubError for
    options that name no item or no key."""

    args: argparse.Namespace

    def section(self) -> str:
        """The section of the item ``patch`` names, checked against how it names the item."""
        args = self.args
        if (args.index is None) == (args.step is None):
            raise HubError("name the item with exactly one of --index and --step")
        section = args.section or "steps"
        if args.step is not None and section != "steps":
            raise HubError("--step names a step; give --index for an item of another section")
        return section

    def pairs(self) -> dict:
        """The ``--set KEY=VALUE`` of ``patch``, each key once and one an item takes."""
        updates = {}
        for pair in self.args.set:
            key, equals, value = pair.partition("=")
            if not equals or not key:
                raise HubError(f"--set takes KEY=VALUE, not {pair!r}")
            if key not in UPDATABLE:
                raise HubError(f"--set {key}: the keys one item can take are {', '.join(UPDATABLE)}")
            if key in updates:
                raise HubError(f"--set {key} is given twice")
            updates[key] = value
        return updates

    def status(self, today: str) -> dict:
        """The keys ``step`` sets: its status, the date it was done or descoped on (``today``) and what is said."""
        args = self.args
        updates = {"status": args.status}
        if args.status in ("done", "descoped") and not args.no_date:
            updates["done_at" if args.status == "done" else "descoped_at"] = today
        if args.evidence is not None:
            updates["evidence"] = args.evidence
        if args.note is not None:
            updates["note"] = args.note
        return updates


class PlanPatch(HubCommand):
    name = "patch"
    help = "set keys of one item of a plan (status, done_at, note, evidence, ...)"
    arguments = (
        PLAN_REF,
        Arg("--section", choices=SECTIONS, help="default: steps"),
        Arg("--index", type=int, help="the item's position in the section, from 0"),
        Arg("--step", help="for steps: the step id instead of --index"),
        Arg("--set", action="append", default=[], required=True, metavar="KEY=VALUE"),
        IF_REVISION_OR_RETRY,
        PROJECT,
    )
    output = WRITTEN

    def run(self, ctx: HubContext) -> Result:
        args, change = ctx.args, ItemChange(ctx.args)
        section = change.section()
        result = patch_item(
            ctx.hub,
            ctx.project,
            checked_plan_id(args.plan),
            section,
            change.pairs(),
            index=args.index,
            step=args.step,
            if_revision=args.if_revision,
        )
        return Result(result, Text(*written_lines(result, "Updated")))


class PlanStep(HubCommand):
    name = "step"
    help = "set the status of one step, as `evo harness step` does"
    arguments = (
        PLAN_REF,
        Arg("step", metavar="STEP", help="the step id"),
        Arg("status", metavar="STATUS", choices=STEP_STATUSES),
        Arg("--evidence", help="what shows the step is in this state, such as repo@sha and a test run"),
        Arg("--note"),
        Arg("--no-date", action="store_true", help="do not set done_at"),
        IF_REVISION_OR_RETRY,
        PROJECT,
    )
    output = WRITTEN

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        today = datetime.now().strftime("%Y-%m-%d")  # the local date, as evo harness step writes it
        updates = ItemChange(args).status(today)
        result = patch_item(
            ctx.hub,
            ctx.project,
            checked_plan_id(args.plan),
            "steps",
            updates,
            step=args.step,
            if_revision=args.if_revision,
        )
        return Result(result, Text(*written_lines(result, f"Step {args.step} is {args.status} in")))


class PlanComplete(HubCommand):
    name = "complete"
    help = "move a plan whose steps are all done or descoped to completed"
    arguments = (PLAN_REF, IF_REVISION, PROJECT)
    output = WRITTEN

    def run(self, ctx: HubContext) -> Result:
        result = complete_plan(ctx.hub, ctx.project, checked_plan_id(ctx.args.plan), ctx.args.if_revision)
        if result["changed"]:
            said = (
                f"Completed plan {result['plan_id']}: revision {result['revision']}. `evo-agents hub plan export` "
                "moves its copy to plans/completed/."
            )
        else:
            said = f"Plan {result['plan_id']} was completed already; nothing changed."
        return Result(result, Text(said))


def closed_row(plan: dict) -> dict:
    """What ``close-finished --json`` prints of a plan of the list, before it tries to complete it."""
    return {key: plan.get(key) for key in ("plan_id", "title", "steps_total", "revision")} | {
        "closed": False,
        "error": None,
    }


class PlanCloseFinished(HubCommand):
    name = "close-finished"
    help = "complete every active plan that has steps, all of them done or descoped"
    arguments = (
        Arg("--dry-run", action="store_true", help="list the plans it would complete, and change none"),
        PROJECT,
    )
    output = returns_array(*CLOSED_KEYS)

    def run(self, ctx: HubContext) -> Result:
        finished = [
            plan
            for plan in Plans.of(ctx).list("active")
            if plan["steps_total"] > 0 and plan["steps_done"] == plan["steps_total"]
        ]
        rows, lines, failed = [], [], False
        for plan in finished:
            row = closed_row(plan)
            if ctx.args.dry_run:
                lines.append(f"would complete {plan['plan_id']}: {plan['steps_total']} steps done")
            else:
                try:
                    result = complete_plan(ctx.hub, ctx.project, plan["plan_id"])
                except HubError as exc:
                    row["error"], failed = str(exc), True
                    lines.append(f"could not complete {plan['plan_id']}: {exc}")
                else:
                    row |= {"closed": result["area"] == "completed", "revision": result["revision"]}
                    lines.append(f"completed {plan['plan_id']}: revision {result['revision']}")
            rows.append(row)
        if not finished:
            lines.append(f"No active plan of {ctx.project} has every step done or descoped.")
        elif not ctx.args.dry_run:
            lines.append("`evo-agents hub plan export` moves their copies to plans/completed/.")
        return Result(rows, Text(*lines), 1 if failed else 0)


class PlanAbandon(HubCommand):
    name = "abandon"
    help = "drop a plan: move it to completed as abandoned (or superseded), with a reason; its steps stay as they are"
    arguments = (
        PLAN_REF,
        Arg("--reason", required=True, metavar="TEXT", help="why the plan is dropped"),
        Arg("--superseded-by", metavar="PLAN", help="the plan that replaces it; status becomes superseded"),
        IF_REVISION,
        PROJECT,
    )
    output = WRITTEN

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        result = abandon_plan(
            ctx.hub, ctx.project, checked_plan_id(args.plan), args.reason, args.superseded_by, args.if_revision
        )
        status = result["body"].get("status", "abandoned")
        if result["changed"]:
            said = (
                f"Plan {result['plan_id']} is {status}: revision {result['revision']}. `evo-agents hub plan export` "
                "moves its copy to plans/completed/."
            )
        else:
            said = f"Plan {result['plan_id']} was {status} already; nothing changed."
        return Result(result, Text(said))


class PlanReopen(HubCommand):
    name = "reopen"
    help = "put a completed plan (abandoned, superseded or finished) back to active"
    arguments = (PLAN_REF, IF_REVISION, PROJECT)
    output = WRITTEN

    def run(self, ctx: HubContext) -> Result:
        result = reopen_plan(ctx.hub, ctx.project, checked_plan_id(ctx.args.plan), ctx.args.if_revision)
        said = (
            f"Reopened plan {result['plan_id']}: revision {result['revision']}. `evo-agents hub plan export` moves "
            "its copy to plans/active/."
        )
        return Result(result, Text(said))


PLAN = Group(
    "plan",
    "plans on the hub, and their read-only copies in a harness",
    [
        PlanImport(),
        PlanList(),
        PlanShow(),
        PlanHistory(),
        PlanHandoff(),
        PlanPut(),
        PlanPatch(),
        PlanStep(),
        PlanComplete(),
        PlanCloseFinished(),
        PlanAbandon(),
        PlanReopen(),
        PlanExport(),
    ],
)
