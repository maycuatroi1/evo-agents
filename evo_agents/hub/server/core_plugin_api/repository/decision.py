"""The queries of the decision API of core plugins (API 1.6 and 1.7): a failed run and its log, the sessions'
corrections, the rules a project accepted, the review queue's dismissals and the decisions on the graph."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def inferred(conn: AsyncConnection, run_id: int) -> bool:
    """Whether the hub guessed the cause of run ``run_id`` (a ``system`` event says ``failure_cause_inferred``)."""
    e = tables.run_events
    guessed = e.c.body["failure_cause_inferred"].astext == "true"
    return bool(
        (await conn.execute(select(func.count()).where(e.c.run_id == run_id, e.c.kind == "system", guessed))).scalar()
    )


async def failed_run(conn: AsyncConnection, project_id: int, run_id: int):
    """Run ``run_id`` of the project with the label of its plan, None without it."""
    r, p = tables.runs, tables.plans
    query = (
        select(r.c.id, r.c.kind, r.c.state, r.c.error, r.c.failure_cause, r.c.plan_id, p.c.label.label("plan_label"))
        .select_from(r.outerjoin(p, (p.c.project_id == r.c.project_id) & (p.c.plan_id == r.c.plan_id)))
        .where(r.c.id == run_id, r.c.project_id == project_id)
    )
    return (await conn.execute(query)).one_or_none()


async def event_tail(conn: AsyncConnection, run_id: int, count: int):
    """The last ``count`` events of run ``run_id``'s log, newest first."""
    e = tables.run_events
    query = select(e.c.seq, e.c.at, e.c.kind, e.c.body).where(e.c.run_id == run_id).order_by(e.c.seq.desc())
    return (await conn.execute(query.limit(count))).all()


async def locked_run(conn: AsyncConnection, project_id: int, run_id: int):
    """The state and cause of run ``run_id`` of the project, its row locked; None without it."""
    r = tables.runs
    query = select(r.c.state, r.c.failure_cause).where(r.c.id == run_id, r.c.project_id == project_id)
    return (await conn.execute(query.with_for_update())).one_or_none()


async def set_cause(conn: AsyncConnection, run_id: int, cause: str) -> None:
    """Give run ``run_id`` the failure cause ``cause``."""
    r = tables.runs
    await conn.execute(r.update().values(failure_cause=cause).where(r.c.id == run_id))


async def session_facets(conn: AsyncConnection, project_id: int, since: datetime, until: datetime | None, count: int):
    """The facets of the project's sessions from ``since`` up to ``until``, newest first, at most ``count``."""
    f = tables.session_facets
    query = select(f.c.session_id, f.c.at, f.c.label, f.c.body).where(
        f.c.project_id == project_id, f.c.kind == "session", f.c.at >= since, f.c.body.is_not(None)
    )
    if until is not None:
        query = query.where(f.c.at < until)
    return (await conn.execute(query.order_by(f.c.at.desc(), f.c.id.desc()).limit(count))).all()


async def accepted_rules(conn: AsyncConnection, project_id: int, count: int):
    """The proposals the project accepted, or whose change merged, with that change, newest first."""
    p, c = tables.proposals, tables.curator_changes
    merged = c.c.merged_at.is_not(None)
    query = (
        select(
            p.c.id.label("proposal_id"),
            p.c.title,
            p.c.summary,
            p.c.label,
            p.c.answered_at,
            c.c.id.label("change_id"),
            c.c.merged_at,
        )
        .select_from(p.outerjoin(c, (c.c.proposal_id == p.c.id) & merged))
        .where(p.c.project_id == project_id, or_(p.c.state == "accepted", c.c.id.is_not(None)))
        .order_by(func.coalesce(c.c.merged_at, p.c.answered_at).desc().nulls_last(), p.c.id.desc())
        .limit(count)
    )
    return (await conn.execute(query)).all()


async def dismissed(conn: AsyncConnection, project_id: int) -> set[str]:
    """The fingerprints of the project's dismissed review items."""
    d = tables.kg_review_dismissals
    return set((await conn.execute(select(d.c.fingerprint).where(d.c.project_id == project_id))).scalars())


async def dismiss(
    conn: AsyncConnection, project_id: int, fingerprint: str, rule: str, reason: str, plugin: str, build
) -> None:
    """Dismiss the project's review item ``fingerprint`` as plugin ``plugin``, in place of a dismissal there was."""
    d = tables.kg_review_dismissals
    saved = pg_insert(d).values(
        project_id=project_id,
        fingerprint=fingerprint,
        rule=rule,
        reason=reason,
        dismissed_by=None,
        dismissed_by_plugin=plugin,
        build_id=build,
    )
    await conn.execute(
        saved.on_conflict_do_update(
            index_elements=[d.c.project_id, d.c.fingerprint],
            set_={
                "rule": saved.excluded.rule,
                "reason": saved.excluded.reason,
                "dismissed_by": None,
                "dismissed_by_plugin": plugin,
                "build_id": saved.excluded.build_id,
                "dismissed_at": func.now(),
            },
        )
    )


async def ontology(conn: AsyncConnection, project_id: int):
    """The ontology of the project's knowledge config, None without one."""
    configs = tables.kg_configs
    return await conn.scalar(select(configs.c.ontology).where(configs.c.project_id == project_id))


async def lock_project(conn: AsyncConnection, project_id: int) -> None:
    """Lock the project's row, so its decisions on the graph are taken one at a time."""
    p = tables.projects
    await conn.execute(select(p.c.id).where(p.c.id == project_id).with_for_update())


async def decision_rev(conn: AsyncConnection, project_id: int) -> int:
    """The project's latest decision rev, 0 before any."""
    e = tables.kg_decision_events
    rev = await conn.scalar(select(func.coalesce(func.max(e.c.rev), 0)).where(e.c.project_id == project_id))
    return rev  # pyright: ignore[reportReturnType]


async def same_decision(
    conn: AsyncConnection, project_id: int, kind: str, src: str, rel: str | None, dst: str | None
) -> int | None:
    """The number of a proposed or active decision of the project that says the same, None without one."""
    d = tables.kg_decisions
    same = select(d.c.number).where(
        d.c.project_id == project_id,
        d.c.kind == kind,
        d.c.src == src,
        d.c.rel.is_not_distinct_from(rel),
        d.c.dst.is_not_distinct_from(dst),
        d.c.state.in_(("proposed", "active")),
    )
    return await conn.scalar(same.limit(1))


async def next_number(conn: AsyncConnection, project_id: int) -> int:
    """The number the project's next decision takes."""
    d = tables.kg_decisions
    number = await conn.scalar(select(func.coalesce(func.max(d.c.number), 0) + 1).where(d.c.project_id == project_id))
    return number  # pyright: ignore[reportReturnType]


async def add_decision(
    conn: AsyncConnection,
    project_id: int,
    number: int,
    kind: str,
    state: str,
    src: str,
    rel: str | None,
    dst: str | None,
) -> int:
    """Make the project's decision ``number``: its id."""
    d = tables.kg_decisions
    query = (
        d.insert()
        .values(project_id=project_id, number=number, kind=kind, state=state, src=src, rel=rel, dst=dst)
        .returning(d.c.id)
    )
    return (await conn.execute(query)).one().id


async def add_decision_event(
    conn: AsyncConnection,
    project_id: int,
    rev: int,
    decision_id: int,
    action: str,
    plugin: str,
    reason: str,
    build,
) -> None:
    """Record ``action`` on decision ``decision_id`` by plugin ``plugin`` as the project's decision rev ``rev``."""
    e = tables.kg_decision_events
    await conn.execute(
        e.insert().values(
            project_id=project_id,
            rev=rev,
            decision_id=decision_id,
            action=action,
            actor_id=None,
            actor_plugin=plugin,
            reason=reason,
            build_id=build,
        )
    )
