"""Which workers of a member can take a run of an agent profile now, and why one cannot: the workers of a member that
serve a project with what each holds, whether it is online and what its last heartbeat says (``workers_of``), and the
reasons a worker is no fit (``worker_problems``). A chat run (``evo_agents.hub.server.chat_runs``) picks its worker
with the first reason, and the candidates to run a failed run again (``service.candidates``) list them all."""

from __future__ import annotations

from sqlalchemy import exists, func, or_, select

from evo_agents.hub import profiles, runs, tables
from evo_agents.hub.server.runs.models import available

OFFLINE = func.make_interval(0, 0, 0, 0, 0, 0, runs.OFFLINE_AFTER_SECONDS)


def workers_of(user_id: int, project_id: int):
    """The workers of ``user_id`` that serve project ``project_id`` and are not revoked, with how many runs each
    holds or has waiting for it (queued, pinned to it), and whether it is online."""
    w, wp, r = tables.workers, tables.worker_projects, tables.runs
    held = (r.c.worker_id == w.c.id) & r.c.state.in_(runs.HELD_STATES)
    waiting = (r.c.pinned_worker_id == w.c.id) & (r.c.state == "queued")
    holding = select(func.count()).where(or_(held, waiting)).scalar_subquery()
    serves = exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == project_id)
    return (
        select(
            w.c.id,
            w.c.name,
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
        )
        .where(w.c.owner_id == user_id, w.c.revoked_at.is_(None), serves)
        .order_by(w.c.id)
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
