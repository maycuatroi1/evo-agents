"""Who may read and write the agent systems of a project, and what a read or a write of one gives (``docs/hub.md``,
Agent systems; ``evo_agents.hub.system_spec``).

Every member of the project reads its systems; writing one needs the admin role on it. A spec is checked
(``system_spec.check`` and ``check_refs``) and pinned (``system_spec.pin``) before it is written: a pinned node that
names no version is pinned at its profile's latest. Writes are audited as ``system.*``. A write refused with the system
as the hub holds it raises ``Refused``, which the route answers with ``SystemConflict``.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import system_spec
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.systems.models.systems import (
    REVISION_CONFLICT,
    Pin,
    ProblemOut,
    Refused,
    SystemCheck,
    SystemChecked,
    SystemDeleted,
    SystemOut,
    SystemPut,
    SystemRevision,
    SystemSummary,
    SystemWritten,
)
from evo_agents.hub.server.systems.repository import store as system_store


def reader_only(access: ProjectAccess) -> None:
    if access.role is None:
        raise Forbidden(f"reading the agent systems of project {access.name} needs a grant on it")


def admin_only(access: ProjectAccess, doing: str) -> None:
    if not holds(access.role, "admin"):
        held = access.role or "no grant"
        raise Forbidden(
            f"{doing} the agent systems of project {access.name} needs the admin role on it; you hold {held}"
        )


def not_found(access: ProjectAccess, slug: str) -> NotFound:
    return NotFound(
        f"project {access.name} has no agent system {slug}: see `evo-agents hub system list --project {access.name}`"
    )


async def reader(conn, user: Principal, project: str) -> ProjectAccess:
    """The caller's access to ``project``, refused (403) without a grant on it."""
    access = await project_access(conn, user, project)
    reader_only(access)
    return access


async def held_system(conn, access: ProjectAccess, slug: str, *, lock: bool = False) -> system_store.System:
    """System ``slug`` of the project, refused (404) when it has none."""
    held = await system_store.one(conn, access.project_id, slug, lock=lock)
    if held is None:
        raise not_found(access, slug)
    return held


def _pins(spec: dict, refs: system_store.Refs) -> list[Pin]:
    found = []
    for pin in system_spec.pins(spec):
        ref = refs.profiles.get(pin["profile"])
        latest = None if ref is None else ref.latest
        version = pin["version"]
        found.append(
            Pin(
                **pin,
                latest_version=latest,
                outdated=latest is not None and version is not None and version < latest,
                deleted=bool(ref and ref.deleted),
            )
        )
    return found


def _problems(problems: list[system_spec.Problem]) -> list[ProblemOut]:
    return [ProblemOut(**p.as_dict()) for p in problems]


def checked(spec, refs: system_store.Refs, slug: str | None) -> tuple[dict, list[system_spec.Problem]]:
    """``spec`` checked and pinned, and what is wrong in it against the project; ``slug`` the system it replaces."""
    found, problems = system_spec.check(spec)
    if system_spec.errors(problems):
        return found, problems
    found = system_spec.pin(found, refs.profiles)
    entries = refs.entries_but(slug) if slug else refs.entries
    return found, problems + system_spec.check_refs(found, refs.profiles, refs.graphs, entries)


def _out(access: ProjectAccess, held: system_store.System, spec: dict, revision: int, refs) -> SystemOut:
    _, problems = system_spec.check(spec)
    problems += system_spec.check_refs(spec, refs.profiles, refs.graphs, refs.entries_but(held.slug))
    return SystemOut(
        project=access.name,
        slug=held.slug,
        name=spec.get("name", held.name),
        revision=revision,
        latest_revision=held.revision,
        spec=spec,
        pins=_pins(spec, refs),
        problems=_problems(problems),
        entry_agent=system_spec.entry_agent(spec),
        can_change=holds(access.role, "admin"),
        created_by=held.created_by,
        updated_by=held.updated_by,
        created_at=held.created_at,
        updated_at=held.updated_at,
    )


async def record(conn, user, access: ProjectAccess, action: str, slug: str, revision: int) -> None:
    await Auditor.of(conn, user, access).record(action, f"{access.name} system={slug} revision={revision}")


async def write_spec(
    conn, user, access: ProjectAccess, slug: str, spec: dict, held: system_store.System | None, refs
) -> int:
    """Store ``spec``, checked and pinned (``checked``) with no error, as the next revision of system ``slug`` (the
    first when ``held`` is None), with its entry agent and the audit row; the revision. The caller checked that
    ``user`` may (the admin role on the project) and that ``held`` is what the system is at now."""
    entry = system_spec.entry_agent(spec)
    entry_id = refs.ids.get(entry) if entry else None
    revision = await system_store.write(conn, access.project_id, slug, spec, entry_id, user.user_id, held)
    action = audit.SYSTEM_CREATE if held is None else audit.SYSTEM_UPDATE
    await record(conn, user, access, action, slug, revision)
    return revision


# Reading


async def listed(conn, user: Principal, project: str) -> list[SystemSummary]:
    """The agent systems of the project, by slug."""
    access = await reader(conn, user, project)
    return [
        SystemSummary(
            slug=s.slug,
            name=s.name,
            revision=s.revision,
            entry_agent=system_spec.entry_agent(s.spec),
            nodes=len(s.spec.get("nodes") or ()),
            edges=len(s.spec.get("edges") or ()),
            updated_by=s.updated_by,
            updated_at=s.updated_at,
            default=s.is_default,
        )
        for s in await system_store.listed(conn, access.project_id)
    ]


async def validated(conn, user: Principal, project: str, body: SystemCheck) -> SystemChecked:
    """What is wrong in a spec against the project, and the spec as PUT would store it; nothing is written."""
    access = await reader(conn, user, project)
    refs = await system_store.refs(conn, access.project_id)
    spec, problems = checked(body.spec, refs, body.slug)
    return SystemChecked(valid=not system_spec.errors(problems), spec=spec, problems=_problems(problems))


async def shown(conn, user: Principal, project: str, slug: str, revision: int | None) -> SystemOut:
    """One agent system, its spec at ``revision`` (the latest when None), its pins and what is wrong in it now."""
    access = await reader(conn, user, project)
    held = await held_system(conn, access, slug)
    spec = held.spec
    if revision is not None and revision != held.revision:
        spec = await system_store.revision_spec(conn, held.id, revision)
        if spec is None:
            raise NotFound(f"agent system {slug} has revisions 1 to {held.revision}, not {revision}")
    refs = await system_store.refs(conn, access.project_id)
    return _out(access, held, spec, revision or held.revision, refs)


async def revisions(conn, user: Principal, project: str, slug: str) -> list[SystemRevision]:
    """The revisions of an agent system, the newest first."""
    access = await reader(conn, user, project)
    held = await held_system(conn, access, slug)
    return [SystemRevision(**row) for row in await system_store.revisions(conn, held.id)]


# Changing


def _stale(access: ProjectAccess, held: system_store.System, given: int | None, refs) -> Refused:
    if not given:
        message = (
            f"agent system {held.slug} is on the hub at revision {held.revision}: pass if_revision {held.revision} "
            "to replace it"
        )
    else:
        message = (
            f"agent system {held.slug} is at revision {held.revision} on the hub, not {given}: someone changed it "
            "since you read it. Read it again and retry; nothing was written"
        )
    current = _out(access, held, held.spec, held.revision, refs)
    return Refused(409, message, code=REVISION_CONFLICT, current=current)


def spec_in(slug: str, body: SystemPut) -> dict:
    """The spec of a PUT of system ``slug``, without its ``slug``; refused (422) when the slug or the spec's slug is
    wrong."""
    if (why := system_spec.check_slug(slug)) is not None:
        raise Unprocessable(why)
    given = body.spec.get("slug") if isinstance(body.spec, dict) else None
    if given is not None and given != slug:
        raise Unprocessable(f"the spec names system {given!r}, the path {slug!r}: they are the same system")
    return {k: v for k, v in body.spec.items() if k != "slug"}


async def put(
    engine: AsyncEngine, user: Principal, project: str, slug: str, body: SystemPut, spec: dict
) -> SystemWritten:
    """Create the agent system, or store ``spec`` (``spec_in`` of ``body``) as its next revision; ``Refused`` when the
    revision is not the one ``body`` names or the spec has an error."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        admin_only(access, "changing")
        held = await system_store.one(conn, access.project_id, slug, lock=True)
        refs = await system_store.refs(conn, access.project_id)
        if (held is None and body.if_revision) or (held is not None and body.if_revision != held.revision):
            if held is None:
                raise NotFound(f"project {access.name} has no agent system {slug} to replace")
            raise _stale(access, held, body.if_revision, refs)
        found, problems = checked(spec, refs, slug)
        if errors := system_spec.errors(problems):
            detail = [p.as_dict() for p in errors]
            count = f"{len(errors)} error{'s' if len(errors) != 1 else ''}"
            raise Refused(422, f"agent system {slug} has {count}: nothing was written", detail=detail)
        if held is not None and held.spec == found:
            out = _out(access, held, held.spec, held.revision, refs)
            return SystemWritten(**out.model_dump(), created=False, changed=False)
        await write_spec(conn, user, access, slug, found, held, refs)
        written = await system_store.one(conn, access.project_id, slug)
        assert written is not None  # write_spec wrote it in this transaction
        refs = await system_store.refs(conn, access.project_id)
        out = _out(access, written, written.spec, written.revision, refs)
        return SystemWritten(**out.model_dump(), created=held is None, changed=True)


async def delete(engine: AsyncEngine, user: Principal, project: str, slug: str) -> SystemDeleted:
    """Delete an agent system with its revisions; its profiles, graphs and runs stay."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        admin_only(access, "deleting")
        held = await held_system(conn, access, slug, lock=True)
        await system_store.remove(conn, held.id)
        await record(conn, user, access, audit.SYSTEM_DELETE, slug, held.revision)
        return SystemDeleted(project=project, deleted=slug)
