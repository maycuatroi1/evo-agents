"""``evo-agents hub plan``: plans on the hub and their read-only copies in a harness.

``import`` pushes every plans/<area>/*.yaml of a harness once (a plan already on the hub with the same digest is
left alone, one that differs is reported, never overwritten). ``put`` creates a plan from a draft, or replaces one
with ``--if-revision``, then rewrites the file as the hub's copy. ``patch``, ``step`` and ``complete`` change one
plan on the hub; ``export`` writes the copies back into the harness (``evo_agents.hub.mirror``). ``list``,
``show`` and ``history`` read, and ``handoff`` prints what a new session needs to take a plan up, as the hub builds
it within a token budget (``evo_agents.hub.handoff``), the same text as the MCP tool plan_handoff.

A write the hub refuses with a revision conflict (someone changed the plan since it was read) is retried for
``patch``, ``step`` and ``complete`` on the new revision, at most ATTEMPTS times, and only when the retry cannot
undo the other change: a patch whose keys the other change set on the same item stops with an error instead, so
no update is lost. ``put`` never retries; a replaced plan has to be read again by the person who wrote it.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness
around the current directory. Standard library and PyYAML only, like the rest of the client.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in, _table, _when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import JsonOutput, json_option, returns_array, returns_object
from evo_agents.hub.handoff import DEFAULT_BUDGET_TOKENS, MAX_BUDGET_TOKENS, MIN_BUDGET_TOKENS
from evo_agents.hub.mirror import (
    PLAN_ID,
    export,
    harness_project,
    harness_root,
    plans_path,
    read_plan,
    render,
    write_copy,
)
from evo_agents.hub.plans import AREAS, SECTIONS, STEP_STATUSES, UPDATABLE, PlanProblem, step_index

ATTEMPTS = 5  # writes sent for one patch or complete before a run of revision conflicts is reported
REVISION_CONFLICT = "revision_conflict"
SHOWN_WARNINGS = 5

# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
PLAN_KEYS = (
    "project",
    "plan_id",
    "area",
    "revision",
    "digest",
    "label",
    "body",
    "created_at",
    "updated_at",
    "updated_by",
)
WRITTEN_KEYS = (*PLAN_KEYS, "created", "changed", "warnings")
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
)
HANDOFF_KEYS = ("project", "plan_id", "revision", "step", "budget_tokens", "tokens", "cut", "text")
WRITTEN = returns_object(*WRITTEN_KEYS, schema="evo_agents__hub__server__plans__Written")
PROJECT_HELP = "hub project (default: hub.project in the harness.yaml around the current directory)"


def _project(args) -> str:
    if getattr(args, "project", None):
        return args.project
    try:
        root = harness_root(None)
    except HubError:
        raise HubError(
            "which project? pass --project, or run this inside a harness whose harness.yaml has hub.project"
        ) from None
    return harness_project(root)


project_of_args = _project  # the public name, for the client modules written since private names stay in their module


def _plan_id(text: str) -> str:
    if not PLAN_ID.fullmatch(text):
        raise HubError(f"{text!r} is not a plan id (a-z, 0-9 and -)")
    return text


def _warnings(result: dict) -> None:
    warnings = result.get("warnings") or []
    for warning in warnings[:SHOWN_WARNINGS]:
        print(f"warning: {warning['path'] or '<root>'}: {warning['message']}")
    if len(warnings) > SHOWN_WARNINGS:
        print(f"... {len(warnings) - SHOWN_WARNINGS} more warning(s); `evo-agents harness validate -v` lists them")


# Operations, each against one ``Hub`` (anything with its ``call`` and ``url``)


def write_plan(hub, method: str, path: str, request: dict, project: str):
    """A plan patch that names the local run of this session, when it has one (``local_sync.run_for``), so the hub
    records the revision as the run's and sets the run's plan and step. A run the hub does not know (404) is left
    out and the write is made as it would be without one: a session never loses a plan write to its own bookkeeping."""
    from evo_agents.hub import local_sync

    run = local_sync.run_for(hub.url, project)
    if run is None:
        return hub.call(method, path, request)
    try:
        return hub.call(method, path, {**request, "run_id": run})
    except HubError as exc:
        if exc.status != 404:
            raise
    return hub.call(method, path, request)


def put_file(hub, path: Path, project: str | None = None, if_revision: int | None = None) -> dict:
    """Push the plan in ``path`` and rewrite ``path`` as the hub's copy; the hub's answer."""
    found = read_plan(path)
    plan_id = _plan_id(str(found.plan_id))
    if project is None:
        project = harness_project(harness_root(path.parent))
    request = {"body": found.body, "area": found.area or "active"}
    if if_revision is not None:
        request["if_revision"] = if_revision
    result = hub.call("PUT", plans_path(project, plan_id), request)
    write_copy(path, render(result["body"], project, result["revision"], result["digest"]))
    return {**result, "project": project}


def _item(body: dict, section: str, index: int | None, step) -> dict | None:
    items = body.get(section)
    try:
        position = step_index(body, step) if step is not None else index
    except PlanProblem:
        return None
    if not isinstance(items, list) or not 0 <= position < len(items) or not isinstance(items[position], dict):
        return None
    return items[position]


def _identity(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in UPDATABLE}


def _check_retry(base: dict, latest: dict, plan_id: str, section: str, index, step, updates: dict) -> None:
    """Raise when retrying on ``latest`` would undo what changed since ``base``: the item moved, or another write
    set one of the keys of ``updates`` to something else."""
    where = f"step {step}" if step is not None else f"{section}[{index}]"
    before = _item(base["body"], section, index, step)
    after = _item(latest["body"], section, index, step)
    if before is None or after is None or (step is None and _identity(before) != _identity(after)):
        raise HubError(
            f"{where} of plan {plan_id} changed place on the hub (revision {latest['revision']}) while this ran; "
            "nothing was written. Check it with `evo-agents hub plan show` and run the command again",
            409,
            "conflict",
        )
    clashes = [key for key in updates if before.get(key) != after.get(key) and after.get(key) != updates[key]]
    if clashes:
        raise HubError(
            f"{', '.join(clashes)} of {where} in plan {plan_id} changed on the hub (revision {latest['revision']}) "
            "after it was read, so it was not overwritten. Check it with `evo-agents hub plan show` and run the "
            "command again to set it anyway",
            409,
            "conflict",
        )


def patch_item(
    hub,
    project: str,
    plan_id: str,
    section: str,
    updates: dict,
    *,
    index: int | None = None,
    step=None,
    if_revision: int | None = None,
    attempts: int = ATTEMPTS,
) -> dict:
    """Set ``updates`` on one item; with ``if_revision``, against that revision only, else on the latest one."""
    path = plans_path(project, plan_id)
    request = {"section": section, "updates": updates}
    request.update({"step": step} if step is not None else {"index": index})
    if if_revision is not None:
        return write_plan(hub, "PATCH", path, {**request, "if_revision": if_revision}, project)
    base = hub.call("GET", path)
    for attempt in range(attempts):
        try:
            return write_plan(hub, "PATCH", path, {**request, "if_revision": base["revision"]}, project)
        except HubError as exc:
            if exc.code != REVISION_CONFLICT or attempt == attempts - 1:
                raise
        latest = hub.call("GET", path)
        _check_retry(base, latest, plan_id, section, index, step, updates)
        base = latest
    raise AssertionError("unreachable")


def complete_plan(hub, project: str, plan_id: str, if_revision: int | None = None, attempts: int = ATTEMPTS) -> dict:
    """Move the plan to completed. The hub checks that every step is done in the revision it completes, so a
    conflict is retried on the latest revision unless ``if_revision`` pins one."""
    path = plans_path(project, plan_id)
    for attempt in range(attempts):
        revision = if_revision if if_revision is not None else hub.call("GET", path)["revision"]
        try:
            return hub.call("POST", f"{path}/complete", {"if_revision": revision})
        except HubError as exc:
            if exc.code != REVISION_CONFLICT or if_revision is not None or attempt == attempts - 1:
                raise
    raise AssertionError("unreachable")


def _post_revisioned(hub, project: str, plan_id: str, action: str, request: dict, if_revision, attempts: int) -> dict:
    path = plans_path(project, plan_id)
    for attempt in range(attempts):
        revision = if_revision if if_revision is not None else hub.call("GET", path)["revision"]
        try:
            return hub.call("POST", f"{path}/{action}", {**request, "if_revision": revision})
        except HubError as exc:
            if exc.code != REVISION_CONFLICT or if_revision is not None or attempt == attempts - 1:
                raise
    raise AssertionError("unreachable")


def abandon_plan(
    hub,
    project: str,
    plan_id: str,
    reason: str,
    superseded_by: str | None = None,
    if_revision: int | None = None,
    attempts: int = ATTEMPTS,
) -> dict:
    """Move the plan to completed as abandoned (superseded with ``superseded_by``), steps untouched."""
    request = {"reason": reason}
    if superseded_by:
        request["superseded_by"] = superseded_by
    return _post_revisioned(hub, project, plan_id, "abandon", request, if_revision, attempts)


def reopen_plan(hub, project: str, plan_id: str, if_revision: int | None = None, attempts: int = ATTEMPTS) -> dict:
    """Put an abandoned plan back in active."""
    return _post_revisioned(hub, project, plan_id, "reopen", {}, if_revision, attempts)


@dataclass
class ImportResult:
    root: Path
    project: str
    plans: list[dict] = field(default_factory=list)  # {plan_id, path, status, revision, message}

    @property
    def failed(self) -> list[dict]:
        return [p for p in self.plans if p["status"] in ("conflict", "failed")]

    def as_json(self) -> dict:
        return {"root": str(self.root), "project": self.project, "plans": self.plans}


def import_plans(hub, root: Path | str | None, project: str | None = None) -> ImportResult:
    """Push every plans/<area>/*.yaml of the harness that the hub does not hold yet."""
    root = harness_root(root)
    project = harness_project(root, project)
    result = ImportResult(root, project)
    held = {plan["plan_id"]: plan for plan in hub.call("GET", plans_path(project))}
    for path in sorted((root / "plans").glob("*/*.yaml")):
        relative = path.relative_to(root).as_posix()
        entry = {"plan_id": path.stem, "path": relative, "status": "failed", "revision": None, "message": ""}
        result.plans.append(entry)
        if path.parent.name not in AREAS:
            entry.update(status="skipped", message=f"plans/{path.parent.name}/ is neither active nor completed")
            continue
        try:
            found = read_plan(path)
        except HubError as exc:
            entry["message"] = str(exc)
            continue
        if found.plan_id != path.stem or not PLAN_ID.fullmatch(path.stem):
            entry["message"] = f"its id is {found.plan_id!r}: a plan file is named after its id, so rename one of them"
            continue
        current = held.get(found.plan_id)
        if current is not None:
            entry["revision"] = current["revision"]
            if (current["digest"], current["area"]) == (found.digest, found.area):
                entry["status"] = "unchanged"
            else:
                entry.update(
                    status="conflict",
                    message=f"differs from revision {current['revision']} on the hub ({current['area']}); push it "
                    f"with `evo-agents hub plan put {relative} --if-revision {current['revision']}` or restore the "
                    "hub's copy with `evo-agents hub plan export`",
                )
            continue
        try:
            answer = hub.call("PUT", plans_path(project, found.plan_id), {"body": found.body, "area": found.area})
        except HubError as exc:
            if exc.status in (401, 403):  # the same for every other file: stop here
                raise
            entry["message"] = str(exc)
            continue
        entry.update(
            status="created" if answer["created"] else "unchanged",
            revision=answer["revision"],
            message=f"{len(answer['warnings'])} warning(s)" if answer["warnings"] else "",
        )
    return result


# Commands


@_client_command
def cmd_import(args) -> int:
    hub, _ = _signed_in()
    result = import_plans(hub, args.root, args.project)
    if args.json:
        _print_json(result.as_json())
        return 1 if result.failed else 0
    for entry in result.plans:
        revision = f" r{entry['revision']}" if entry["revision"] else ""
        message = f": {entry['message']}" if entry["message"] else ""
        print(f"{entry['status']:<9} {entry['path']}{revision}{message}")
    counts = {status: sum(1 for p in result.plans if p["status"] == status) for status in ("created", "unchanged")}
    print(
        f"{len(result.plans)} file(s) for project {result.project}: {counts['created']} created, "
        f"{counts['unchanged']} already on the hub, {len(result.failed)} not pushed"
    )
    if not result.failed:
        print(f"Next: `evo-agents hub plan export {result.root}` writes the copies the hub keeps.")
    return 1 if result.failed else 0


@_client_command
def cmd_list(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    path = plans_path(project) + (f"?area={args.area}" if args.area else "")
    plans = hub.call("GET", path)
    if args.json:
        _print_json(plans)
        return 0
    rows = [
        (
            p["plan_id"],
            p["area"],
            p["revision"],
            f"{p['steps_done']}/{p['steps_total']}",
            _when(p["updated_at"]),
            p["updated_by"],
        )
        for p in plans
    ]
    _table(("PLAN", "AREA", "REV", "STEPS DONE", "UPDATED (UTC)", "BY"), rows)
    return 0


@_client_command
def cmd_show(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    path = plans_path(project, _plan_id(args.plan))
    plan = hub.call("GET", f"{path}/revisions/{args.revision}" if args.revision else path)
    if args.json:
        _print_json(plan)
        return 0
    sys.stdout.write(render(plan["body"], project, plan["revision"], plan["digest"]))
    return 0


@_client_command
def cmd_handoff(args) -> int:
    if not MIN_BUDGET_TOKENS <= args.budget_tokens <= MAX_BUDGET_TOKENS:
        raise HubError(f"--budget-tokens is {MIN_BUDGET_TOKENS} to {MAX_BUDGET_TOKENS}, not {args.budget_tokens}")
    hub, _ = _signed_in()
    query = {"budget_tokens": args.budget_tokens, **({"step": args.step} if args.step is not None else {})}
    handoff = hub.call("GET", f"{plans_path(_project(args), _plan_id(args.plan))}/handoff?{urlencode(query)}")
    if args.json:
        _print_json(handoff)
        return 0
    sys.stdout.write(handoff["text"])
    return 0


def _by(revision: dict) -> str:
    """Who wrote a revision: the member, and the run that wrote it as them (an author run, or a local session's)."""
    run_id = revision.get("run_id")
    return revision["actor"] + (f" (run #{run_id})" if run_id else "")


@_client_command
def cmd_history(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    revisions = hub.call("GET", f"{plans_path(project, _plan_id(args.plan))}/revisions")
    if args.json:
        _print_json(revisions)
        return 0
    rows = [(r["revision"], r["area"], _when(r["created_at"]), _by(r), r["summary"]) for r in revisions]
    _table(("REV", "AREA", "WHEN (UTC)", "BY", "SUMMARY"), rows)
    return 0


@_client_command
def cmd_put(args) -> int:
    hub, _ = _signed_in()
    path = Path(args.file).expanduser()
    result = put_file(hub, path, args.project, args.if_revision)
    if args.json:
        _print_json(result)
        return 0
    plan = f"plan {result['plan_id']} of project {result['project']}"
    if result["created"]:
        print(f"Created {plan} at revision 1; {path} is now its copy.")
    elif result["changed"]:
        print(f"Replaced {plan}: revision {result['revision']}; {path} is now its copy.")
    else:
        print(f"The hub already holds {path} as {plan}, revision {result['revision']}; {path} is now its copy.")
    _warnings(result)
    return 0


def _report(result: dict, what: str) -> None:
    plan = f"plan {result['plan_id']}"
    if result["changed"]:
        print(f"{what} {plan}: revision {result['revision']}. `evo-agents hub plan export` updates its copy.")
    else:
        print(f"{plan} already holds that (revision {result['revision']}); nothing changed.")
    _warnings(result)


def _updates(pairs: list[str]) -> dict:
    updates = {}
    for pair in pairs:
        key, equals, value = pair.partition("=")
        if not equals or not key:
            raise HubError(f"--set takes KEY=VALUE, not {pair!r}")
        if key not in UPDATABLE:
            raise HubError(f"--set {key}: the keys one item can take are {', '.join(UPDATABLE)}")
        if key in updates:
            raise HubError(f"--set {key} is given twice")
        updates[key] = value
    return updates


@_client_command
def cmd_patch(args) -> int:
    hub, _ = _signed_in()
    if (args.index is None) == (args.step is None):
        raise HubError("name the item with exactly one of --index and --step")
    section = args.section or "steps"
    if args.step is not None and section != "steps":
        raise HubError("--step names a step; give --index for an item of another section")
    result = patch_item(
        hub,
        _project(args),
        _plan_id(args.plan),
        section,
        _updates(args.set),
        index=args.index,
        step=args.step,
        if_revision=args.if_revision,
    )
    if args.json:
        _print_json(result)
        return 0
    _report(result, "Updated")
    return 0


@_client_command
def cmd_step(args) -> int:
    hub, _ = _signed_in()
    updates = {"status": args.status}
    if args.status in ("done", "descoped") and not args.no_date:
        updates["done_at" if args.status == "done" else "descoped_at"] = datetime.now().strftime(
            "%Y-%m-%d"
        )  # the local date, as evo harness step writes it
    if args.evidence is not None:
        updates["evidence"] = args.evidence
    if args.note is not None:
        updates["note"] = args.note
    result = patch_item(
        hub, _project(args), _plan_id(args.plan), "steps", updates, step=args.step, if_revision=args.if_revision
    )
    if args.json:
        _print_json(result)
        return 0
    _report(result, f"Step {args.step} is {args.status} in")
    return 0


@_client_command
def cmd_complete(args) -> int:
    hub, _ = _signed_in()
    result = complete_plan(hub, _project(args), _plan_id(args.plan), args.if_revision)
    if args.json:
        _print_json(result)
        return 0
    if result["changed"]:
        print(
            f"Completed plan {result['plan_id']}: revision {result['revision']}. `evo-agents hub plan export` moves "
            "its copy to plans/completed/."
        )
    else:
        print(f"Plan {result['plan_id']} was completed already; nothing changed.")
    return 0


@_client_command
def cmd_abandon(args) -> int:
    hub, _ = _signed_in()
    result = abandon_plan(hub, _project(args), _plan_id(args.plan), args.reason, args.superseded_by, args.if_revision)
    if args.json:
        _print_json(result)
        return 0
    status = result["body"].get("status", "abandoned")
    if result["changed"]:
        print(
            f"Plan {result['plan_id']} is {status}: revision {result['revision']}. `evo-agents hub plan export` "
            "moves its copy to plans/completed/."
        )
    else:
        print(f"Plan {result['plan_id']} was {status} already; nothing changed.")
    return 0


@_client_command
def cmd_reopen(args) -> int:
    hub, _ = _signed_in()
    result = reopen_plan(hub, _project(args), _plan_id(args.plan), args.if_revision)
    if args.json:
        _print_json(result)
        return 0
    print(
        f"Reopened plan {result['plan_id']}: revision {result['revision']}. `evo-agents hub plan export` moves "
        "its copy to plans/active/."
    )
    return 0


@_client_command
def cmd_export(args) -> int:
    hub, _ = _signed_in()
    result = export(hub, Path(args.root).expanduser() if args.root else None, args.project, commit=args.commit)
    if args.json:
        _print_json(result.as_json())
        return 0
    for entry in result.plans:
        if entry["status"] != "unchanged":
            print(f"wrote    {entry['path']} (revision {entry['revision']})")
    for path in result.removed:
        print(f"removed  {path}")
    for note in result.notes:
        print(f"note: {note}")
    unchanged = sum(1 for p in result.plans if p["status"] == "unchanged")
    print(
        f"{len(result.plans)} plan(s) of project {result.project} in {result.root}: "
        f"{len(result.plans) - unchanged} written, {unchanged} unchanged, {len(result.removed)} removed."
    )
    if args.commit:
        print(f"Committed the copies as {result.commit[:12]}." if result.commit else "The copies match HEAD.")
    return 0


def _common(parser, output: JsonOutput) -> None:
    parser.add_argument("--project", help=PROJECT_HELP)
    json_option(parser, output)


def _register_reads(psub) -> None:
    """``list``, ``show``, ``history`` and ``handoff``."""
    listed = psub.add_parser("list", help="the plans of a project you can see, with revision and progress")
    listed.add_argument("--area", choices=AREAS)
    _common(listed, returns_array(*SUMMARY_KEYS, schema="PlanSummary"))
    listed.set_defaults(func=cmd_list)

    show = psub.add_parser("show", help="a plan as its copy in git reads (with --json, as the hub answers)")
    show.add_argument("plan", metavar="PLAN")
    show.add_argument("--revision", type=int, help="an earlier revision")
    revision_body = returns_object(*REVISION_KEYS, "label", "body", schema="RevisionBody")
    _common(show, returns_object(*PLAN_KEYS, schema="Plan", variants=[("--revision", revision_body)]))
    show.set_defaults(func=cmd_show)

    history = psub.add_parser("history", help="the revisions of a plan: who changed it, when and what")
    history.add_argument("plan", metavar="PLAN")
    _common(history, returns_array(*REVISION_KEYS, "run_id", schema="Revision"))
    history.set_defaults(func=cmd_history)

    handoff = psub.add_parser(
        "handoff", help="what a new session needs to take a plan up: goal, steps done with decisions, steps left"
    )
    handoff.add_argument("plan", metavar="PLAN")
    handoff.add_argument("--step", metavar="KEY", help="for this step: the evidence of the steps it depends on first")
    handoff.add_argument(
        "--budget-tokens",
        type=int,
        default=DEFAULT_BUDGET_TOKENS,
        metavar="N",
        help=f"the most tokens the text may take, {MIN_BUDGET_TOKENS} to {MAX_BUDGET_TOKENS} "
        f"(default {DEFAULT_BUDGET_TOKENS})",
    )
    _common(handoff, returns_object(*HANDOFF_KEYS, schema="PlanHandoff"))
    handoff.set_defaults(func=cmd_handoff)


def _register_writes(psub) -> None:
    """``put``, ``patch``, ``step``, ``complete``, ``abandon`` and ``reopen``."""
    put = psub.add_parser(
        "put", help="create a plan from a file, or replace it with --if-revision; the file becomes the hub's copy"
    )
    put.add_argument("file", metavar="FILE")
    put.add_argument("--if-revision", type=int, help="the revision on the hub this file replaces")
    _common(put, WRITTEN)
    put.set_defaults(func=cmd_put)

    patch = psub.add_parser("patch", help="set keys of one item of a plan (status, done_at, note, evidence, ...)")
    patch.add_argument("plan", metavar="PLAN")
    patch.add_argument("--section", choices=SECTIONS, help="default: steps")
    patch.add_argument("--index", type=int, help="the item's position in the section, from 0")
    patch.add_argument("--step", help="for steps: the step id instead of --index")
    patch.add_argument("--set", action="append", default=[], required=True, metavar="KEY=VALUE")
    patch.add_argument("--if-revision", type=int, help="fail on a conflict instead of retrying on the latest revision")
    _common(patch, WRITTEN)
    patch.set_defaults(func=cmd_patch)

    step = psub.add_parser("step", help="set the status of one step, as `evo harness step` does")
    step.add_argument("plan", metavar="PLAN")
    step.add_argument("step", metavar="STEP", help="the step id")
    step.add_argument("status", metavar="STATUS", choices=STEP_STATUSES)
    step.add_argument("--evidence", help="what shows the step is in this state, such as repo@sha and a test run")
    step.add_argument("--note")
    step.add_argument("--no-date", action="store_true", help="do not set done_at")
    step.add_argument("--if-revision", type=int, help="fail on a conflict instead of retrying on the latest revision")
    _common(step, WRITTEN)
    step.set_defaults(func=cmd_step)

    complete = psub.add_parser("complete", help="move a plan whose steps are all done or descoped to completed")
    complete.add_argument("plan", metavar="PLAN")
    complete.add_argument("--if-revision", type=int)
    _common(complete, WRITTEN)
    complete.set_defaults(func=cmd_complete)

    abandon = psub.add_parser(
        "abandon",
        help="drop a plan: move it to completed as abandoned (or superseded), with a reason; its steps stay as they are",
    )
    abandon.add_argument("plan", metavar="PLAN")
    abandon.add_argument("--reason", required=True, metavar="TEXT", help="why the plan is dropped")
    abandon.add_argument("--superseded-by", metavar="PLAN", help="the plan that replaces it; status becomes superseded")
    abandon.add_argument("--if-revision", type=int)
    _common(abandon, WRITTEN)
    abandon.set_defaults(func=cmd_abandon)

    reopen = psub.add_parser("reopen", help="put an abandoned or superseded plan back to active")
    reopen.add_argument("plan", metavar="PLAN")
    reopen.add_argument("--if-revision", type=int)
    _common(reopen, WRITTEN)
    reopen.set_defaults(func=cmd_reopen)


def register_plans(hsub) -> None:
    plan = hsub.add_parser("plan", help="plans on the hub, and their read-only copies in a harness")
    psub = plan.add_subparsers(dest="plan_command", required=True)

    imported = psub.add_parser(
        "import", help="push every plans/*/*.yaml of a harness the hub does not hold yet (same digest: left alone)"
    )
    imported.add_argument("root", metavar="HARNESS_ROOT", nargs="?", help="the harness (default: the cwd)")
    _common(imported, returns_object("root", "project", "plans"))
    imported.set_defaults(func=cmd_import)

    _register_reads(psub)
    _register_writes(psub)

    exported = psub.add_parser(
        "export", help="write the copy of every plan of the project into the harness, and commit them with --commit"
    )
    exported.add_argument("root", metavar="HARNESS_ROOT", nargs="?", help="the harness (default: the cwd)")
    exported.add_argument(
        "--commit", action="store_true", help="commit the copies that changed, and nothing else in the checkout"
    )
    _common(exported, returns_object("root", "project", "plans", "removed", "notes", "commit"))
    exported.set_defaults(func=cmd_export)
