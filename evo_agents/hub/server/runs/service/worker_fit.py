"""Which workers of a member can take a run of an agent profile now, and why one cannot: the workers of a member that
serve a project, and those other members share in it (``shared_in``), with what each holds, whether it is online and
what its last heartbeat says (``workers_of``), and the reasons a worker is no fit (``worker_problems``). A chat run
(``evo_agents.hub.server.chat_runs``) picks its worker with the first reason, and the candidates to run a failed run
again (``service.candidates``) list them all."""

from __future__ import annotations

from functools import cache

from sqlalchemy import and_, exists, func, or_, select

from evo_agents.hub import org, profiles, runs, tables
from evo_agents.hub.access import ROLES, has_role
from evo_agents.hub.server.runs.models import available

OFFLINE = func.make_interval(0, 0, 0, 0, 0, 0, runs.OFFLINE_AFTER_SECONDS)
WRITER_ROLES = tuple(role for role in ROLES if has_role(role, "writer"))


def writes(user_id, project_id):
    """Whether member ``user_id`` holds writer on project ``project_id`` now, as a condition (each a value or a
    column)."""
    g = tables.grants
    return exists().where(g.c.user_id == user_id, g.c.project_id == project_id, g.c.role.in_(WRITER_ROLES))


@cache
def _seat_chains():
    """Each member's live primary seat (not ended, its end day not passed) with every unit from the seat's up to the
    root, one row (user_id, unit_id) each: a recursive CTE, built once so every query of a statement shares it."""
    s, u = tables.org_seats, tables.org_units
    seats = (
        select(s.c.user_id, s.c.unit_id)
        .where(
            s.c.is_primary,
            s.c.user_id.is_not(None),
            s.c.ended_at.is_(None),
            or_(s.c.ends_on.is_(None), s.c.ends_on >= func.current_date()),
        )
        .cte("seat_chains", recursive=True)
    )
    up = select(seats.c.user_id, u.c.parent_id).join_from(seats, u, u.c.id == seats.c.unit_id)
    return seats.union(up.where(u.c.parent_id.is_not(None)))


def unit_admits(worker_id, principal_id):
    """Whether the units worker ``worker_id`` is narrowed to (``tables.worker_shares``) let it take a run of member
    ``principal_id`` (each a value or a column): it has none, or the member's live primary seat is in one of them or
    below one, with no unit above the seat archived nor a cohort above it ended (``org.Chart.ended``)."""
    ws, u, chain = tables.worker_shares, tables.org_units, _seat_chains()
    narrowed = exists().where(ws.c.worker_id == worker_id)
    seated = exists().where(
        ws.c.worker_id == worker_id, chain.c.user_id == principal_id, chain.c.unit_id == ws.c.unit_id
    )
    ended_unit = or_(u.c.archived_at.is_not(None), and_(u.c.kind == org.COHORT, u.c.ends_on < func.current_date()))
    ended = exists().where(chain.c.user_id == principal_id, chain.c.unit_id == u.c.id, ended_unit)
    return or_(~narrowed, and_(seated, ~ended))


def shared_in(project_id, principal_id):
    """Whether the worker of the row (``tables.workers``) is one its owner shares in project ``project_id`` with member
    ``principal_id`` (each a value or a column): shared, serving the project, its owner holding writer there now, and,
    when it is narrowed to units of the org chart, the member seated in one (``unit_admits``; ``docs/workers.md``,
    Shared workers)."""
    w, wp = tables.workers, tables.worker_projects
    serves = exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == project_id)
    return w.c.shared & serves & writes(w.c.owner_id, project_id) & unit_admits(w.c.id, principal_id)


def workers_of(user_id: int, project_id: int):
    """The workers of ``user_id`` that serve project ``project_id`` and are not revoked, then those other members share
    in it (``shared_in``), with the owner of each, how many runs each holds or has waiting for it (queued, pinned to
    it), how many of the runs it holds are another member's than ``user_id`` (``others``: a shared worker takes no run
    of ``user_id`` while it holds one), and whether it is online."""
    w, wp, r, users = tables.workers, tables.worker_projects, tables.runs, tables.users
    held = (r.c.worker_id == w.c.id) & r.c.state.in_(runs.HELD_STATES)
    waiting = (r.c.pinned_worker_id == w.c.id) & (r.c.state == "queued")
    holding = select(func.count()).where(or_(held, waiting)).scalar_subquery()
    others = select(func.count()).where(held, r.c.dispatched_by != user_id).scalar_subquery()
    serves = exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == project_id)
    owner = select(users.c.login).where(users.c.id == w.c.owner_id).scalar_subquery()
    return (
        select(
            w.c.id,
            w.c.name,
            w.c.owner_id,
            owner.label("owner"),
            w.c.labels,
            w.c.runtimes,
            w.c.checkouts,
            w.c.features,
            w.c.run_kinds,
            w.c.slots,
            w.c.dispatch_from,
            w.c.agent_version,
            w.c.drained_at,
            w.c.plugins,
            (w.c.last_heartbeat_at >= func.now() - OFFLINE).label("online"),
            holding.label("holding"),
            others.label("others"),
        )
        .where(or_(w.c.owner_id == user_id, shared_in(project_id, user_id)), w.c.revoked_at.is_(None), serves)
        .order_by(w.c.owner_id != user_id, w.c.id)
    )


def takes_kind(row, kind: str) -> bool:
    """Whether the daemon of worker ``row`` runs runs of ``kind``: a step run any daemon does, a plan run one of
    ``runs.PLAN_RUN_AGENT`` or later, the others one whose heartbeat names the kind."""
    if kind == "step":
        return True
    if kind == "plan":
        return runs.takes_plan_runs(row.agent_version)
    return kind in (row.run_kinds or ())


def usable_runtimes(row, profile) -> list[str]:
    """The runtimes worker ``row`` reports available that ``profile`` allows, in ``runs.RUNTIMES`` order."""
    allow = profiles.allowed(profile.permissions, "runtime")
    usable = [name for name in runs.RUNTIMES if available((row.runtimes or {}).get(name))]
    return [name for name in usable if allow is None or name in allow]


def worker_problems(row, profile, via: str, kind: str = "chat") -> list[str]:
    """Why worker ``row`` (``workers_of``) cannot take a run of ``kind`` of ``profile`` dispatched with ``via`` now,
    each reason once, in the order they are checked; none when it can."""
    packs = profiles.packs_unfit(
        profile.packs, worker_id=row.id, agent_version=row.agent_version, features=row.features, plugins=row.plugins
    )
    checks = (
        (not row.online, "is offline"),
        (row.drained_at is not None, "is draining"),
        (not takes_kind(row, kind), f"runs evo-agents {row.agent_version or '?'}, which takes no {kind} run"),
        (row.dispatch_from == "web" and via != "web", "takes runs dispatched from the web only"),
        (row.holding >= row.slots, "has no free slot"),  # its runs and the runs queued for it
        (row.others > 0, "holds a run of another member, and a shared worker runs one member's runs at a time"),
        (not row.checkouts, "has no checkout, and claims nothing"),
        (
            not profiles.allows_worker(profile.permissions, row.name, row.labels or ()),
            f"is not one @{profile.handle} allows",
        ),
        (not usable_runtimes(row, profile), f"has no runtime @{profile.handle} allows"),
        (
            profiles.needs_worker(profile.permissions, profile.instructions)
            and runs.AGENT_PROFILES not in (row.features or ()),
            "has a daemon that does not apply agent profiles",
        ),
        (packs is not None, f"cannot build the agent packs of @{profile.handle}: {packs}"),
    )
    return [why for failed, why in checks if failed]


def unfit_worker(row, profile, via: str, kind: str = "chat") -> str | None:
    """The first of ``worker_problems``, or None when worker ``row`` can take the run."""
    return next(iter(worker_problems(row, profile, via, kind)), None)
