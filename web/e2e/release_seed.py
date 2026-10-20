"""The daily release of a project as the Releases specs read it, made on the stack's database (``hub_stack.py`` POST
/releases/seed).

The stack's hub has no GitHub App, so no check of it reads a tag and no cycle of it opens by itself. ``seed_release``
plays what the hub's job would have done for the project's release schedule, which the spec set through the API: two
cycles that ended (one published v0.24.0 after its cut run, one closed on a pause) and a cycle open now, whose cut run
pushed 0.25.0, which the hub tagged; its workflow failed one test, and its fix run is queued. With ``stopped`` no fix
run is queued and the open cycle stops on that test the way the watch stops one (``release_cycles.stop_cycle``): it asks
the owner a decision and posts a thread in the project's triage chat. Its runs are queued through the hub's own ``queue_run`` and ended on
the spec's worker, so they read as any release run does. Two checks are kept, a scheduled one that found nothing and the
one that opened the cycle. Names are fictional (northwind).
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

MERGES = {"v0.24.0": "a" * 40, "v0.25.0": "e" * 40}
PUSHED = {"0.24.0": "1" * 40, "0.25.0": "f" * 40}
BASE, TARGET = "b" * 40, "c" * 40
RED_TEST = "tests/hub/test_paths.py::test_a_written_file_ends_lines_in_lf[windows]"
WORKFLOW_URL = "https://github.com/example-org/api/actions/runs/7001"
PULL_URL = "https://github.com/example-org/api/pull/12"


def seed_release(dsn: str, body: dict) -> dict:
    """The cycles of ``body["project"]``'s release schedule (see the module), its runs ended on worker
    ``body["worker_id"]``; the ids of what it made. LookupError when the project has no schedule."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def once() -> dict:
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool)
        try:
            return await _seed(engine, str(body["project"]), int(body["worker_id"]), bool(body.get("stopped")))
        finally:
            await engine.dispose()

    return asyncio.run(once())


async def _schedule(conn, project: str):
    from sqlalchemy import select

    from evo_agents.hub import tables

    s, p = tables.release_schedules, tables.projects
    row = (await conn.execute(select(s).join(p, p.c.id == s.c.project_id).where(p.c.name == project))).one_or_none()
    if row is None:
        raise LookupError(f"project {project} has no release schedule: set one first")
    return row


async def _cycle(conn, row, day: date, **values) -> int:
    from sqlalchemy import insert

    from evo_agents.hub import tables

    c = tables.release_cycles
    cycle = {"project_id": row.project_id, "schedule_id": row.id, "start": "cut", "owner_id": row.owner_id}
    cycle |= {"local_day": day, "target_ref": row.target_ref, "budget_usd": float(row.cycle_budget_usd)}
    cycle |= {"base_tag": "v0.23.0", "base_commit": BASE, "target_commit": TARGET, "changed_paths": ["web/"], **values}
    return (await conn.execute(insert(c).values(**cycle).returning(c.c.id))).scalar_one()


async def _ran(conn, run_id: int, worker_id: int, cost: float, version: str, tag: str) -> None:
    """Run ``run_id`` ended done on ``worker_id`` at ``cost``, having pushed ``version``, which the hub tagged."""
    from sqlalchemy import func, update

    from evo_agents.hub import tables

    r, rr = tables.runs, tables.release_runs
    ran = {"worker_id": worker_id, "leased_at": func.now(), "started_at": func.now(), "finished_at": func.now()}
    await conn.execute(update(r).values(**ran, state="done", usage={"total_cost_usd": cost}).where(r.c.id == run_id))
    landed = {"version": version, "branch": f"release/{version}", "head_commit": PUSHED[version], "pull_url": PULL_URL}
    landed |= {"merge_commit": MERGES[tag], "tagged": tag, "tagged_at": func.now(), "land_state": "tagged"}
    decision = f"Decision: {version}, the next minor: web/ changed."
    await conn.execute(update(rr).values(**landed, decision=decision).where(rr.c.run_id == run_id))


async def _tag(conn, cycle_id: int, run_id: int, tag: str, ci_state: str, **values) -> None:
    from sqlalchemy import func, insert

    from evo_agents.hub import tables

    row = {"cycle_id": cycle_id, "tag": tag, "commit": MERGES[tag], "run_id": run_id, "ci_state": ci_state}
    row |= {"workflow_run_id": 7001, "workflow_url": WORKFLOW_URL, "run_attempt": 1, "checked_at": func.now()}
    await conn.execute(insert(tables.release_tags).values(**row, **values))


async def _end(conn, cycle_id: int, state: str, **values) -> None:
    from sqlalchemy import func, update

    from evo_agents.hub import tables

    c = tables.release_cycles
    await conn.execute(update(c).values(state=state, ended_at=func.now(), **values).where(c.c.id == cycle_id))


async def _check(conn, row, day: date, outcome: str, reason: str, **values) -> None:
    from sqlalchemy import insert

    from evo_agents.hub import tables

    check = {"project_id": row.project_id, "schedule_id": row.id, "trigger": "schedule", "local_day": day}
    check |= {"outcome": outcome, "reason": reason, "base_tag": "v0.24.0", "base_commit": MERGES["v0.24.0"]}
    await conn.execute(insert(tables.release_checks).values(**check, target_commit=TARGET, **values))


async def _seed(engine, project: str, worker_id: int, stopped: bool) -> dict:
    from evo_agents.hub.server.release_cycles import queue_run, stop_cycle

    today = date.today()
    async with engine.begin() as conn:
        row = await _schedule(conn, project)
        published = await _cycle(conn, row, today - timedelta(days=3))
        cut = await queue_run(conn, published, "cut")
        await _ran(conn, cut, worker_id, 3.1, "0.24.0", "v0.24.0")
        await _tag(conn, published, cut, "v0.24.0", "passed")
        await _end(conn, published, "published")
        closed = await _cycle(conn, row, today - timedelta(days=2), base_tag="v0.24.0", base_commit=MERGES["v0.24.0"])
        await _end(conn, closed, "closed", stop_reason=f"the release schedule of {project} was paused by its owner")
        await _check(
            conn, row, today - timedelta(days=1), "unchanged", "nightly changes no packaging path since v0.24.0"
        )
        current = await _cycle(conn, row, today, base_tag="v0.24.0", base_commit=MERGES["v0.24.0"])
        cut_now = await queue_run(conn, current, "cut")
        await _ran(conn, cut_now, worker_id, 8.15, "0.25.0", "v0.25.0")
        failures = {
            "ids": [RED_TEST],
            "jobs": [{"job": "ci / test-windows (3.12)", "step": "Run pytest"}],
            "infra": False,
        }
        await _tag(conn, current, cut_now, "v0.25.0", "failed", failures=failures)
        fix = None if stopped else await queue_run(conn, current, "fix", red_tag="v0.25.0")
        opened = {"cycle_id": current, "run_id": cut_now, "changed_paths": ["web/"]}
        await _check(conn, row, today, "opened", "web/ changed since v0.24.0", **opened)
    if stopped:
        async with engine.begin() as conn:
            reason = f"{RED_TEST} failed at v0.25.0 and at the tag before it: the same test red at two tags in a row"
            await stop_cycle(conn, current, reason, "same_test")
    return {"published": published, "closed": closed, "cycle": current, "cut": cut_now, "fix": fix}
