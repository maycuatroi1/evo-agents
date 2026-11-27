"""The autonomy policy of a project, kept as revisions (plan gitlab-merge-queue, criterion a2; the keys in
``evo_agents.hub.autonomy``): reading a revision, the list of them and the keys two of them differ in, and writing the
next one.

Writes of one project's policy wait for each other (a transaction-level advisory lock). Each revision adds an audit
row ``policy.set`` naming the revision and the keys whose value it changed, never a value. A revision that turns
``loop.enabled`` on or off turns the project's Loop on or off in the same transaction (``evo_agents.hub.server.loop.
state``). ``effective_policy`` is what the hub's own jobs read.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import autonomy
from evo_agents.hub.server import audit
from evo_agents.hub.server.autonomy.models.policies import (
    MAX_AUDITED_KEYS_CHARS,
    AutonomyPolicy,
    PolicyChange,
    PolicyComparison,
    PolicyRevision,
)
from evo_agents.hub.server.autonomy.repository import policies as store
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Forbidden, NotFound, refuse
from evo_agents.hub.server.base.revisions import at_revision
from evo_agents.hub.server.loop import state as loop_state
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds

log = logging.getLogger(__name__)


# Reading


async def _at(conn: AsyncConnection, access: ProjectAccess, revision: int):
    """Revision ``revision`` of the project's policy; None for revision 0; 404 when it has no such revision."""
    if revision == 0:
        return None
    row = await store.revision(conn, access.project_id, revision)
    if row is None:
        raise NotFound(f"the policy of project {access.name} has no revision {revision}")
    return row


async def effective_policy(conn: AsyncConnection, project_id: int) -> dict:
    """The project's policy now, every key at its value or its default (``autonomy.effective``)."""
    row = await store.newest(conn, project_id)
    return autonomy.effective(None if row is None else row.body, await store.project_repos(conn, project_id))


def _view(project: str, row, repos: dict[str, autonomy.RepoFacts]) -> AutonomyPolicy:
    if row is None:
        effective = autonomy.effective(None, repos)
        return AutonomyPolicy(
            project=project, revision=0, policy={}, effective=effective, note=None, created_by=None, created_at=None
        )
    return AutonomyPolicy(
        project=project,
        revision=row.revision,
        policy=row.body,
        effective=autonomy.effective(row.body, repos),
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )


async def view_at(conn: AsyncConnection, access: ProjectAccess, revision: int | None = None) -> AutonomyPolicy:
    """The project's policy at ``revision``, the newest when None."""
    row = await store.newest(conn, access.project_id) if revision is None else await _at(conn, access, revision)
    return _view(access.name, row, await store.project_repos(conn, access.project_id))


async def reader(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    """The access of ``user`` to ``project``; 403 without a grant on it."""
    access = await project_access(conn, user, project)
    if access.role is None:
        raise Forbidden(f"reading the policy of project {access.name} needs a grant on it")
    return access


async def revisions(conn: AsyncConnection, access: ProjectAccess) -> list[PolicyRevision]:
    """Every revision of the project's policy, the newest first, each with the keys whose value it changed."""
    rows = await store.revisions(conn, access.project_id)
    repos = await store.project_repos(conn, access.project_id)
    listed, before = [], autonomy.effective(None, repos)
    for row in rows:
        after = autonomy.effective(row.body, repos)
        changed = [key for key, _, _ in autonomy.changes(before, after)]
        listed.append(
            PolicyRevision(
                revision=row.revision,
                note=row.note,
                created_by=row.created_by,
                created_at=row.created_at,
                changed=changed,
            )
        )
        before = after
    return listed[::-1]


async def compare(
    conn: AsyncConnection, access: ProjectAccess, from_revision: int, to_revision: int | None
) -> PolicyComparison:
    """The keys whose value differs between two revisions of the project's policy, each as it reads now."""
    before = await view_at(conn, access, from_revision)
    after = await view_at(conn, access, to_revision)
    changes = [
        PolicyChange(key=key, before=old, after=new)
        for key, old, new in autonomy.changes(before.effective, after.effective)
    ]
    return PolicyComparison(
        project=access.name, from_revision=before.revision, to_revision=after.revision, changes=changes
    )


# Writing


def refusal(user: Principal, access: ProjectAccess) -> str | None:
    """Why ``user`` may not write the project's policy; None when they may."""
    if user.acts_for_run is not None:
        return "the agent of a run never writes the policy: @builder proposes a change, which an admin approves"
    if holds(access.role, "admin"):
        return None
    held = access.role or "no grant"
    return f"writing the policy of project {access.name} needs the admin role on it; {user.login} holds {held}"


def refuse_writer(user: Principal, access: ProjectAccess) -> None:
    """403 with ``refusal`` when ``user`` may not write the project's policy."""
    refused = refusal(user, access)
    if refused is not None:
        raise Forbidden(refused)


def _keys_text(keys: list[str]) -> str:
    text = ",".join(keys)
    return text if len(text) <= MAX_AUDITED_KEYS_CHARS else text[: MAX_AUDITED_KEYS_CHARS - 3] + "..."


async def write(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    body: dict,
    *,
    note: str | None = None,
    if_revision: int | None = None,
    via: str = "",
) -> tuple[int, bool]:
    """Write ``body`` as the next revision of the project's policy, once it is right (422 naming each problem
    otherwise) and while the policy is at ``if_revision`` (409 otherwise); (the revision now, whether one was
    written). ``via`` ends the audit target (`` proposal=<id>``). The caller checked ``refusal``."""
    await store.lock(conn, access.project_id)
    held = await store.newest(conn, access.project_id)
    now = 0 if held is None else held.revision
    at_revision(f"the policy of project {access.name}", now, if_revision)
    repos = await store.project_repos(conn, access.project_id)
    refuse(autonomy.problems(body, repos))
    before = {} if held is None else held.body
    if body == before:
        return now, False
    after = autonomy.effective(body, repos)
    keys = [key for key, _, _ in autonomy.changes(autonomy.effective(before, repos), after)]
    values = {
        "project_id": access.project_id,
        "revision": now + 1,
        "body": body,
        "note": " ".join((note or "").split()) or None,
        "created_by": user.user_id,
    }
    await store.add(conn, values)
    target = f"{access.name}/policy revision={now + 1} keys={_keys_text(keys)}{via}"
    await Auditor.of(conn, user, access).record(audit.POLICY_SET, target)
    await loop_state.policy_written(conn, access.project_id, bool(after["loop"]["enabled"]))
    log.info("autonomy policy written", extra={"project": access.name, "revision": now + 1, "keys": len(keys)})
    return now + 1, True
