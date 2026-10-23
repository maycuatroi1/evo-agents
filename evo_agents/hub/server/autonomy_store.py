"""The autonomy levels on the hub (plan autonomy-levels, criteria a2 and a3): a person's own preference as the table
autonomy_prefs holds it, every write a new revision with its ``autonomy.pref_set`` audit row, and the effective
autonomy of a person, alone or running an agent profile on a repo, from the seven sources ``org.autonomy_sources``
lists (``autonomy_levels.effective``).

A preference above the ceilings is stored as it is: the effective level is the lowest of the sources, so the ceiling
still decides. Who sees whose autonomy is ``org.view_refusal``: the person, a lead of a unit above them, a hub admin.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import autonomy, autonomy_levels, org, tables
from evo_agents.hub.server import audit, autonomy_policies
from evo_agents.hub.server.autonomy_models import AutonomyClass, AutonomyView, PrefSet, PrefWritten
from evo_agents.hub.server.org_store import load, lock, profile_by_handle, today, user_by_login
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


def actor(user: Principal) -> org.Actor:
    return org.Actor(user_id=user.user_id, login=user.login, admin=user.admin)


async def newest_pref(conn: AsyncConnection, user_id: int):
    """(revision, level, overrides) of the person's newest preference; None when they set none."""
    p = tables.autonomy_prefs
    query = (
        select(p.c.revision, p.c.level, p.c.overrides)
        .where(p.c.user_id == user_id)
        .order_by(p.c.revision.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


def _words(classes: dict[str, str], reset: bool) -> str:
    return "".join(f" {klass}={mode}" for klass, mode in classes.items()) + (" reset" if reset else "")


async def write_pref(conn: AsyncConnection, user: Principal, body: PrefSet) -> PrefWritten:
    """The next revision of the caller's own preference, with its audit row; the agent of a run is refused."""
    if user.scope is not None:
        raise HTTPException(403, "the agent of a run never changes an autonomy preference")
    if body.level is None and not body.classes and not body.reset:
        raise HTTPException(422, "a preference sets a level, a class or a reset; nothing changed")
    problem = autonomy_levels.overrides_problem(body.classes)
    if problem is not None:
        raise HTTPException(422, f"{problem}; nothing changed")
    await lock(conn)  # two writes of one person's preference take turns
    before = await newest_pref(conn, user.user_id)
    previous = None if before is None else before.level
    kept = {} if before is None or body.reset else dict(before.overrides)
    level = body.level or (autonomy_levels.HIGHEST if body.reset else previous) or autonomy_levels.HIGHEST
    merged = {**kept, **body.classes}
    classes = {klass: merged[klass] for klass in autonomy_levels.CLASSES if klass in merged}
    revision = 1 if before is None else before.revision + 1
    values = {"user_id": user.user_id, "revision": revision, "level": level, "overrides": classes}
    await conn.execute(insert(tables.autonomy_prefs).values(values))
    was = "none" if previous is None else previous
    target = f"pref {user.login} level={was}->{level}{_words(body.classes, body.reset)} revision={revision}"
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=audit.AUTONOMY_PREF_SET, target=target
    )
    log.info("autonomy preference set", extra={"login": user.login, "revision": revision})
    return PrefWritten(login=user.login, revision=revision, level=level, previous=previous, classes=classes)


async def _policy(conn: AsyncConnection, project: str, project_id: int, repo: str) -> autonomy_levels.Source:
    """Source 4: the project policy of ``repo``; 404 when the project lists no such repo."""
    repos = (await autonomy_policies.effective_policy(conn, project_id))[autonomy.REPOS]
    if repo not in repos:
        raise HTTPException(404, f"project {project} lists no repo {repo}")
    held = repos[repo]
    return autonomy_levels.policy_source(project, repo, held["mode"], held["prod_release"])


async def _profile(conn: AsyncConnection, project: str, agent: str) -> tuple[str, int]:
    found = await profile_by_handle(conn, agent, project)
    if found is None:
        raise HTTPException(404, f"project {project} has no agent profile {agent}")
    return found.handle, found.id


async def view(
    conn: AsyncConnection,
    user: Principal,
    login: str | None,
    *,
    project: str | None = None,
    agent: str | None = None,
    repo: str | None = None,
) -> AutonomyView:
    """The effective autonomy of ``login`` (the caller without one), running ``agent`` on ``repo`` of ``project`` when
    those are named; 403 when the caller may not see it, 404 for a name that is unknown or hidden from them."""
    if (agent is not None or repo is not None) and project is None:
        raise HTTPException(422, "an agent or a repo is named within a project: name the project")
    if login is None:
        user_id, principal = user.user_id, user.login
    else:
        found = await user_by_login(conn, login)
        if found is None:
            raise HTTPException(404, f"no user {login}")
        user_id, principal = found.id, found.login
    chart, day = await load(conn), await today(conn)
    refused = org.view_refusal(chart, actor(user), user_id, principal, day)
    if refused is not None:
        raise HTTPException(403, refused)
    policy = profile = None
    if project is not None:
        access = await project_access(conn, user, project)
        policy = None if repo is None else await _policy(conn, project, access.project_id, repo)
        profile = None if agent is None else await _profile(conn, project, agent)
    pref = await newest_pref(conn, user_id)
    held = autonomy_levels.pref_source(principal, *((None,) if pref is None else (pref.level, pref.overrides)))
    sources = org.autonomy_sources(chart, user_id, day, held, policy=policy, profile=profile)
    reached = autonomy_levels.effective(sources)
    return AutonomyView(
        principal=principal,
        project=project,
        agent=None if profile is None else profile[0],
        level=reached.level,
        classes={
            klass: AutonomyClass(mode=choice.mode, source=choice.source, detail=choice.detail)
            for klass, choice in reached.classes.items()
        },
        environments=None if reached.environments is None else list(reached.environments),
        workers=None if reached.workers is None else list(reached.workers),
    )
