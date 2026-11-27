"""The SQL of the health of a project's knowledge graph (``evo_agents.hub.server.kg_health``): the projects due a
measure, what a measure reads of the knowledge config, the builds, the plans and the review queue, the measures kept
in kg_health, and what the night's figures and the route list of them."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, exists, func, insert, literal, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import hub_settings
from evo_agents.hub.server.kg.models.health import HEALTH_EVERY, NIGHT_SHIFT, RETRY_AFTER

# Which projects


async def due(conn: AsyncConnection, now: datetime | None) -> list:
    """A row per project to measure now: its id, name, and the owner of its night shift's first schedule."""
    s, p, u, h, cfg = tables.schedules, tables.projects, tables.users, tables.kg_health, tables.kg_configs
    moment = func.now() if now is None else literal(now)
    wait = case((h.c.error.is_(None), literal(HEALTH_EVERY)), else_=literal(RETRY_AFTER))
    recent = exists().where(h.c.project_id == p.c.id, h.c.measured_at > moment - wait)
    query = (
        select(p.c.id.label("project_id"), p.c.name.label("project"), s.c.owner_id, u.c.login.label("owner"))
        .ext(distinct_on(p.c.id))
        .join_from(s, p, p.c.id == s.c.project_id)
        .join(u, u.c.id == s.c.owner_id)
        .where(s.c.kind == NIGHT_SHIFT, hub_settings.feature_on("curator", s.c.project_id))  # pyright: ignore[reportArgumentType]
        .where(exists().where(cfg.c.project_id == p.c.id), ~recent)
        .order_by(p.c.id, s.c.id)
    )
    return list((await conn.execute(query)).all())


# What a measure reads


async def knowledge(conn: AsyncConnection, project_id: int):
    """The knowledge config of the project (kg_configs.knowledge); None without one."""
    cfg = tables.kg_configs
    return (await conn.execute(select(cfg.c.knowledge).where(cfg.c.project_id == project_id))).scalar_one_or_none()


async def source_files(conn: AsyncConnection, project_id: int) -> dict:
    """The kg_source_files rows of the project, by source."""
    held = tables.kg_source_files
    return {found.source: found for found in await conn.execute(select(held).where(held.c.project_id == project_id))}


async def origins(conn: AsyncConnection, project_id: int) -> dict:
    """The origin of each repo of the project, by name."""
    pr = tables.project_repos
    return dict((await conn.execute(select(pr.c.name, pr.c.origin).where(pr.c.project_id == project_id))).all())


async def plan_bodies(conn: AsyncConnection, project_id: int, readable) -> list[tuple[str, dict]]:
    """(plan_id, body) of the project's plans among ``readable``, by plan id; those without a body aside."""
    p = tables.plans
    rows = await conn.execute(
        select(p.c.plan_id, p.c.body)
        .where(p.c.project_id == project_id, p.c.plan_id.in_(readable))
        .order_by(p.c.plan_id)
    )
    return [(row.plan_id, row.body) for row in rows if isinstance(row.body, dict)]


async def previous_eval(conn: AsyncConnection, project_id: int):
    """The eval of the project's latest measure that did not fail; None without one."""
    h = tables.kg_health
    query = (
        select(h.c.body["eval"])
        .where(h.c.project_id == project_id, h.c.error.is_(None))
        .order_by(h.c.measured_at.desc(), h.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).scalar_one_or_none()


async def dismissed(conn: AsyncConnection, project_id: int) -> set[str]:
    """The fingerprints of the review items a writer dismissed in the project."""
    d = tables.kg_review_dismissals
    return set((await conn.execute(select(d.c.fingerprint).where(d.c.project_id == project_id))).scalars())


async def add_measure(conn: AsyncConnection, values: dict) -> int:
    """Keep a measure in kg_health: its id."""
    h = tables.kg_health
    return (await conn.execute(insert(h).values(**values).returning(h.c.id))).scalar_one()


# The night's figures and the route


def unchanged(b):
    """Whether a build of ``b`` (kg_builds) has the content hash of the project's succeeded build before it: it
    changed nothing."""
    before = b.alias("before")
    previous = (
        select(before.c.content_hash)
        .where(before.c.project_id == b.c.project_id, before.c.status == "succeeded", before.c.id < b.c.id)
        .order_by(before.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return func.coalesce(b.c.content_hash == previous, False).label("unchanged")


async def latest_measure(conn: AsyncConnection, project_id: int, until: datetime):
    """(measured_at, body) of the project's latest measure before ``until`` that did not fail; None without one."""
    h = tables.kg_health
    query = (
        select(h.c.measured_at, h.c.body)
        .where(h.c.project_id == project_id, h.c.error.is_(None), h.c.measured_at < until)
        .order_by(h.c.measured_at.desc(), h.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


async def latest_covered(conn: AsyncConnection, project_id: int, until: datetime):
    """(id, finished_at, coverage) of the project's latest successful build before ``until`` with a coverage."""
    b = tables.kg_builds
    query = (
        select(b.c.id, b.c.finished_at, b.c.coverage)
        .where(b.c.project_id == project_id, b.c.status == "succeeded", b.c.coverage.is_not(None))
        .where(b.c.finished_at < until)
        .order_by(b.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


async def span_builds(conn: AsyncConnection, project_id: int, since: datetime, until: datetime) -> list:
    """The project's builds that finished in [since, until), the latest first, with whether each changed nothing."""
    b = tables.kg_builds
    query = (
        select(b.c.id, b.c.status, b.c.finished_at, unchanged(b), b.c.coverage)
        .where(b.c.project_id == project_id, b.c.finished_at >= since, b.c.finished_at < until)
        .order_by(b.c.id.desc())
    )
    return list((await conn.execute(query)).all())


async def run_kg(conn: AsyncConnection, project_id: int, plans: list[str], since: datetime, until: datetime) -> list:
    """(run_id, kg) of the facets of the runs that ended in the span, of a plan in ``plans`` or review runs."""
    f, r = tables.session_facets, tables.runs
    query = (
        select(f.c.run_id, f.c.body["kg"].label("kg"))
        .join_from(f, r, r.c.id == f.c.run_id)
        .where(f.c.project_id == project_id, f.c.kind == "run", f.c.body.is_not(None))
        .where(r.c.kind != "local", r.c.plan_id.in_(plans) | (r.c.kind == "review"))
        .where(r.c.finished_at >= since, r.c.finished_at < until)
        .order_by(f.c.run_id)
    )
    return list((await conn.execute(query)).all())


async def measures(conn: AsyncConnection, project_id: int, limit: int) -> list:
    """The project's latest ``limit`` measures, newest first."""
    h = tables.kg_health
    query = (
        select(h.c.measured_at, h.c.build_id, h.c.error, h.c.body)
        .where(h.c.project_id == project_id)
        .order_by(h.c.measured_at.desc(), h.c.id.desc())
        .limit(limit)
    )
    return list((await conn.execute(query)).all())


async def succeeded_builds(conn: AsyncConnection, project_id: int, limit: int) -> list:
    """The project's latest ``limit`` successful builds, newest first, with whether each changed nothing."""
    b = tables.kg_builds
    query = (
        select(b.c.id, b.c.finished_at, unchanged(b), b.c.coverage)
        .where(b.c.project_id == project_id, b.c.status == "succeeded")
        .order_by(b.c.id.desc())
        .limit(limit)
    )
    return list((await conn.execute(query)).all())
