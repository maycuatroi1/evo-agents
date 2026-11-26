"""The writes of a project's probes: an admin adds, edits, retires and requeues one (each audited as curator.probe),
and the agent of a review run adds one to a merged change that has none, or in place of the change's probe the weekly
report found not reproduced, or stale, two weeks in a row (``service.fixes.replace_assignment``)."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import probes as probe_shape
from evo_agents.hub.access import has_role
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.curator.repository.charter import charter_row
from evo_agents.hub.server.probes.models.fixes import Assignment
from evo_agents.hub.server.probes.models.probe import PROBE, Probe, ProbeAdd, ProbeEdit, ProbeRetire, ReviewProbe, view
from evo_agents.hub.server.probes.models.runs import ProbeRequeue
from evo_agents.hub.server.probes.repository import probes as repository
from evo_agents.hub.server.probes.service import fixes, runs
from evo_agents.hub.server.probes.service.probes import held_row, shown, store
from evo_agents.hub.server.probes.service.report import local_today
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.proposals import held_review_run, project_repos, writer_of
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.security import Principal

NOT_REPLACEABLE = (
    "probe {probe} is not an active probe of change {change} that was not reproduced, or stale, on one model in the two "
    "weekly reports before this run's week: the hub asks for a replacement only then; nothing was recorded"
)


def need_admin(access: ProjectAccess, what: str) -> None:
    """403 unless the caller holds the admin role on the project."""
    if not has_role(access.role, "admin"):
        raise Forbidden(
            f"{what} of project {access.name} needs the admin role on it; you hold {access.role or 'no role'}"
        )


async def change_target(conn: AsyncConnection, project_id: int, project: str, change_id: int):
    """The change ``change_id`` of the project (id, state, repo, proposal_id) with its proposal's label; 404 without."""
    row = await repository.change_target(conn, project_id, change_id)
    if row is None:
        raise NotFound(f"project {project} has no change {change_id}")
    return row


async def _audited(conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int, what: str) -> None:
    await Auditor.of(conn, user, access).record(PROBE, f"{access.name} probe:{probe_id} {what}")


async def _target(conn: AsyncConnection, access: ProjectAccess, body: ProbeAdd) -> tuple[dict, dict, str]:
    """(the change and proposal, the label, the audit's words) of the change or the proposal ``body`` adds a probe to."""
    if body.change_id is not None:
        change = await change_target(conn, access.project_id, access.name, body.change_id)
        if (found := await repository.active_on_change(conn, change.id)) is not None:
            raise Conflict(f"change {change.id} has active probe #{found}: retire it first")
        return {"change_id": change.id, "proposal_id": change.proposal_id}, change.label, f"add change:{change.id}"
    proposal = await repository.proposal_target(conn, access.project_id, body.proposal_id)
    if proposal is None:
        raise NotFound(f"project {access.name} has no proposal {body.proposal_id}")
    if (found := await repository.active_on_proposal(conn, proposal.id)) is not None:
        raise Conflict(f"proposal {proposal.id} has active probe #{found}: retire it first")
    change_id = await repository.change_of_proposal(conn, proposal.id)
    return {"proposal_id": proposal.id, "change_id": change_id}, proposal.label, f"add proposal:{proposal.id}"


async def add(conn: AsyncConnection, access: ProjectAccess, user: Principal, body: ProbeAdd) -> Probe:
    """Add a probe to a change or a proposal of the project; its admins alone may."""
    need_admin(access, "adding a probe")
    if body.repo not in await project_repos(conn, access.project_id):
        raise Unprocessable(f"project {access.name} has no repo {body.repo}; nothing was recorded")
    target, label, what = await _target(conn, access, body)
    probe_id = await store(
        conn, project_id=access.project_id, probe=body, source="admin", label=label, user_id=user.user_id, **target
    )
    await _audited(conn, access, user, probe_id, what)
    return await shown(conn, access, user, probe_id)


async def edit(conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int, body: ProbeEdit) -> Probe:
    """Change the repo, commit, task or verify of an active probe; the project's admins alone may."""
    need_admin(access, "editing a probe")
    row = await held_row(conn, access, user, probe_id, None, lock=True)
    if row.state != "active":
        raise Conflict(f"probe {probe_id} is {row.state}: only an active probe is edited")
    if body.repo is not None and body.repo not in await project_repos(conn, access.project_id):
        raise Unprocessable(f"project {access.name} has no repo {body.repo}; nothing was changed")
    values = {{"commit": "base_sha"}.get(key, key): getattr(body, key) for key in sorted(body.model_fields_set)}
    await repository.revise(conn, probe_id, values, user_id=user.user_id)
    await _audited(conn, access, user, probe_id, f"edit {','.join(sorted(body.model_fields_set))}")
    return await shown(conn, access, user, probe_id)


async def retire(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int, body: ProbeRetire
) -> Probe:
    """Retire a probe: no probe run takes it from then on; the project's admins alone may."""
    need_admin(access, "retiring a probe")
    row = await held_row(conn, access, user, probe_id, None, lock=True)
    if row.state == "retired":
        raise Conflict(f"probe {probe_id} is retired already")
    values = {"state": "retired", "retired_at": func.now(), "retired_note": body.note}
    await repository.revise(conn, probe_id, values, user_id=user.user_id)
    await _audited(conn, access, user, probe_id, "retire")
    return await shown(conn, access, user, probe_id)


async def requeue(conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int) -> ProbeRequeue:
    """Measure an active probe again this ISO week (``service.runs.requeue``); the project's admins alone may."""
    need_admin(access, "requeueing a probe")
    row = await held_row(conn, access, user, probe_id, None, lock=True)
    if row.state != "active":
        raise Conflict(f"probe {probe_id} is {row.state}: only an active probe is measured again")
    charter = await charter_row(conn, access.project_id)
    today = await local_today(conn, dict(charter.body) if charter is not None else {})
    found = await runs.requeue(conn, probe_id, probe_shape.week_of(today))
    await _audited(conn, access, user, probe_id, f"requeue week:{found.week.isoformat()} rows:{len(found.requeued)}")
    return found


# A review run's probe of a merged change


async def _replaceable(conn: AsyncConnection, run, access: ProjectAccess, change_id: int, probe_id: int) -> Assignment:
    """The replace assignment of probe ``probe_id`` of change ``change_id`` the weekly probe report gives review run
    ``run`` (``fixes.replace_assignment``); 422 without one."""
    night = await fixes.run_night(conn, run.id)
    found = await fixes.replace_assignment(conn, access, run.owner, night, probe_id)
    if found is None or found.change_id != change_id:
        raise Unprocessable(NOT_REPLACEABLE.format(probe=probe_id, change=change_id))
    return found


async def record(conn: AsyncConnection, user: Principal, run_id: int, body: ReviewProbe) -> Probe:
    """A probe from the agent of the review run this worker holds, for a merged change that has none."""
    run = await held_review_run(conn, user, run_id)
    access = await writer_of(conn, run)
    change = await change_target(conn, run.project_id, run.project, body.change_id)
    if change.state != "merged":
        raise Conflict(f"change {change.id} is {change.state}: a review run adds a probe to a merged change alone")
    found = await repository.active_on_change(conn, change.id)
    assigned = None
    if body.replaces is not None:
        assigned = await _replaceable(conn, run, access, change.id, body.replaces)
    elif found is not None:
        raise Conflict(f"change {change.id} has probe #{found} already")
    if body.repo not in await project_repos(conn, run.project_id):
        raise Unprocessable(f"project {run.project} has no repo {body.repo}; nothing was recorded")
    if assigned is not None and body.replaces is not None:
        words = "stale" if assigned.stale else "not reproduced"
        note = f"{words} two weekly reports in a row: review run #{run_id} replaced it"
        values = {"state": "retired", "retired_at": func.now(), "retired_note": note}
        await repository.revise(conn, body.replaces, values, user_id=run.owner.user_id)
    probe_id = await store(
        conn,
        project_id=run.project_id,
        probe=body,
        source="review",
        label=change.label,
        proposal_id=change.proposal_id,
        change_id=change.id,
        user_id=run.owner.user_id,
        run_id=run_id,
        replaces=body.replaces,
    )
    what = f"probe #{probe_id} of change #{change.id}"
    what += f", in place of probe #{body.replaces}" if body.replaces is not None else ""
    await write_event(conn, run_id, {"text": what, "probe": probe_id})
    return view(access.name, await repository.by_id(conn, probe_id), shown=False)
