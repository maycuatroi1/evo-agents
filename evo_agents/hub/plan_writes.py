"""Plan writes against the hub, shared by ``evo-agents hub plan`` (``cli.plan``), the MCP tools of the hub
(``evo_agents.hub.server.mcp``) and ``evo-agents worker plan``: ``put_file``, ``patch_item``, ``complete_plan``,
``abandon_plan``, ``reopen_plan`` and ``import_plans``, each against one ``Hub`` (anything with its ``call`` and
``url``), and the keys of the hub's answers they print.

A write the hub refuses with a revision conflict (someone changed the plan since it was read) is retried for
``patch``, ``step`` and ``complete`` on the new revision, at most ATTEMPTS times, and only when the retry cannot
undo the other change (``check_retry``): a patch whose keys the other change set on the same item stops with an
error instead, so no update is lost. ``put`` never retries. Standard library and PyYAML only, like the rest of the
client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.clikit.json_output import returns_object
from evo_agents.hub.client import HubError
from evo_agents.hub.mirror import PLAN_ID, harness_project, harness_root, plans_path, read_plan, render, write_copy
from evo_agents.hub.plans import AREAS, UPDATABLE, PlanProblem, step_index

ATTEMPTS = 5  # writes sent for one patch or complete before a run of revision conflicts is reported
REVISION_CONFLICT = "revision_conflict"

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
WRITTEN = returns_object(*WRITTEN_KEYS, schema="evo_agents__hub__server__plans__Written")


def checked_plan_id(text: str) -> str:
    if not PLAN_ID.fullmatch(text):
        raise HubError(f"{text!r} is not a plan id (a-z, 0-9 and -)")
    return text


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
    plan_id = checked_plan_id(str(found.plan_id))
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
    if not isinstance(items, list) or position is None or not 0 <= position < len(items):
        return None
    if not isinstance(items[position], dict):
        return None
    return items[position]


def _identity(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in UPDATABLE}


def check_retry(base: dict, latest: dict, plan_id: str, section: str, index, step, updates: dict) -> None:
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
        check_retry(base, latest, plan_id, section, index, step, updates)
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
