"""The night shift's breaker of dependencies: a run of the schedule that failed of an infra cause
(``curator.INFRA_CAUSES``) holds off the kinds of run that use the dependency it points at, not the whole shift.

``infra_of`` reads, under the schedule's row lock the gate took, the runs of the schedule that ended in the last
``curator.INFRA_LOOKBACK`` and gives back the dependencies failing now (``curator.outages``): the hub, which every kind
uses, or a runtime with its account, which the kinds the charter runs on it use (``curator.kind_runtimes``; the Judge's
runtime the hub picks, and it takes another while its own is held off). While a dependency waits its backoff the job
hub.fire_schedules queues none of those kinds; once it is over, the next run of them goes as the probe. Before the
probe of a dependency that failed of ``account_auth`` or ``rate_limit`` (``triage_rules.NEW_ACCOUNT``), the accounts
the failed run took cool down until the probe after it may go, so the probe's lease takes another account of the
charter when one is ready. A dependency that used the day's ``max_probes_per_day`` tells the schedule's owner once,
with a notice of kind run_failed, until it works again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, tables, triage_rules

log = logging.getLogger(__name__)

INFRA_NOTICE = "run_failed"  # the notice kind of a dependency that used its probes of the day
INFRA_REASON = "the night shift's probe of a failing dependency"  # the reason of the cooldown it gives an account


@dataclass(frozen=True)
class Infra:
    """The dependencies of a schedule failing at ``now``."""

    now: datetime
    outages: dict[str, curator.Outage] = field(default_factory=dict)

    def waiting(self, dependency: str) -> curator.Outage | None:
        """The outage of ``dependency`` when its kinds of run wait now, else None."""
        found = self.outages.get(dependency)
        return found if found is not None and found.held(self.now) else None

    @property
    def hub(self) -> curator.Outage | None:
        return self.waiting(curator.HUB)

    @property
    def avoid(self) -> frozenset[str]:
        """The runtimes no run is queued on now."""
        prefix = "runtime:"
        return frozenset(
            name.removeprefix(prefix) for name in self.outages if name.startswith(prefix) and self.waiting(name)
        )

    def holds(self, charter: dict, kind: str) -> bool:
        """Whether the night shift queues no run of ``kind`` now: the hub, or a runtime the charter runs it on, waits."""
        return self.hub is not None or any(name in self.avoid for name in curator.kind_runtimes(charter, kind))


async def infra_of(conn: AsyncConnection, due, now: datetime) -> Infra:
    """The dependencies of schedule ``due`` failing at ``now``; tells the owner of each that used its probes of the
    day, once, and cools the accounts of a failed run whose probe is due and takes another (see the module)."""
    r = tables.runs
    rows = await conn.execute(
        select(r.c.id, r.c.queued_at, r.c.finished_at, r.c.runtime, r.c.state, r.c.failure_cause).where(
            r.c.schedule_id == due.id,
            r.c.state.in_(("done", "failed")),
            r.c.finished_at >= now - curator.INFRA_LOOKBACK,
        )
    )
    ended = [
        curator.Ended(row.id, row.queued_at, row.finished_at, row.runtime, row.state == "failed", row.failure_cause)
        for row in rows
    ]
    steps, most = curator.infra_settings(due.body)
    found = Infra(now, curator.outages(ended, now, steps, most, key=str(due.id)))
    for outage in found.outages.values():
        if outage.exhausted:
            await _tell_owner(conn, due, outage, most)
        elif not outage.held(now):
            await _cool_accounts(conn, outage, steps, due.id)
    return found


async def _tell_owner(conn: AsyncConnection, due, outage: curator.Outage, most: int) -> None:
    """Tell the owner of schedule ``due`` once that ``outage`` used its probes of the day: once for each run of failures
    in a row (none since ``outage.since`` names the dependency)."""
    from evo_agents.hub.server.notifications import notify  # it reads runs through the routes that import this module

    n = tables.notifications
    told = exists().where(
        n.c.user_id == due.owner_id,
        n.c.project_id == due.project_id,
        n.c.notice_kind == INFRA_NOTICE,
        n.c.details["dependency"].astext == outage.dependency,
        n.c.created_at >= outage.since,
    )
    if (await conn.execute(select(told))).scalar():
        return
    body = (
        f"The night shift of {due.project} held off the runs that use {outage.dependency}: it failed {outage.failures} "
        f"times in a row, the last one run #{outage.last_run_id} of cause {outage.cause}, and its {outage.probes} "
        f"probes of the last day (max_probes_per_day {most}) failed too. The other kinds of run go on; the next probe "
        f"goes at {outage.retry_at.isoformat(timespec='minutes')}. Fix the cause (an account's login, a runtime on the "
        "pool's nodes, the hub) and the next probe closes it."
    )
    await notify(
        conn,
        user_id=due.owner_id,
        kind="notice",
        notice_kind=INFRA_NOTICE,
        project_id=due.project_id,
        run_id=outage.last_run_id,
        title=f"The night shift of {due.project} holds off {outage.dependency}: its probes of the day failed",
        body=body,
        details={
            "dependency": outage.dependency,
            "failure_cause": outage.cause,
            "failures": outage.failures,
            "probes": outage.probes,
            "max_probes_per_day": most,
            "retry_at": outage.retry_at.isoformat(),
        },
        link=f"/p/{due.project}/curator",
    )
    log.warning(
        "night shift dependency used its probes",
        extra={"project": due.project, "dependency": outage.dependency, "run_id": outage.last_run_id},
    )


async def _cool_accounts(conn: AsyncConnection, outage: curator.Outage, steps: tuple[int, ...], key: int) -> None:
    """Cool the accounts the failed run of ``outage`` took until the probe after the next may go, so the next probe's
    lease takes another account when the charter has one ready (``account_choice``)."""
    from evo_agents.hub.server.account_choice import _cool  # it imports the routes that import this module

    if outage.cause not in triage_rules.NEW_ACCOUNT:
        return
    ra = tables.run_accounts
    taken = (await conn.execute(select(ra.c.secret_id).where(ra.c.run_id == outage.last_run_id))).scalars().all()
    until = outage.retry_at + curator.backoff(outage.failures + 1, steps, f"{key}:{outage.dependency}:next")
    for secret_id in sorted(set(taken)):
        await _cool(conn, secret_id, until, f"{INFRA_REASON}: {outage.cause} in run #{outage.last_run_id}")
