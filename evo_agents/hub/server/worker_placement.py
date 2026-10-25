"""Where a run may go (plan worker-pools, criteria a2 and a3): the labels and taints of a worker, whether a node of a
pool is ready, and how a run's selector and tolerations meet them. The claim (``runs.service.claims``), the rerun
candidates, the machine of a chat run and the worker axis of a profile all read them from here, so each counts them
the same way.

- Labels: a worker's own, its pool's and pool=<name> (``node_labels``), and the ones the hub gives it from its
  heartbeat (``auto_labels``): os=<os>, arch=<arch>, runtime=<name> for each runtime it has, and feature=browser when
  it has one. All lowercased.
- Taints: its own, which its owner sets (PATCH /v1/workers/{id}), and its pool's.
- Ready: a worker of no pool always is. A node of a pool is while its newest doctor report is fresh (no older than
  ``runs.DOCTOR_FRESH_SECONDS``, 48 hours) and has no high finding but those whose codes its pool accepts; a node that
  is not takes no run, pinned or not.
- A run goes to a worker whose labels hold its whole selector and each of whose taints it tolerates; a run pinned to
  the worker by name goes there whatever its taints. A run with no selector on a worker with no taint goes as before.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Text, and_, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.runs.models import available
from evo_agents.hub.server.runs.models.placement import POOL_KEY, normalized

POOL_LABEL_KEY = POOL_KEY


def _fresh(doctor_at):
    """``worker_doctor.doctor_fresh``, which this module cannot import (worker_doctor imports notifications, which
    import the claim that imports this): a report no older than ``runs.DOCTOR_FRESH_SECONDS``."""
    return doctor_at >= func.now() - func.make_interval(0, 0, 0, 0, 0, 0, runs.DOCTOR_FRESH_SECONDS)


# The labels of a node


def _pool_field(w, column):
    p = tables.worker_pools
    return select(column).where(p.c.id == w.c.pool_id).scalar_subquery()


def node_labels(w=None):
    """The labels a worker row of ``w`` (tables.workers by default) has from its pool: the pool's labels and
    pool=<name>; an empty array for a worker of no pool. Add them to w.labels for all its labels."""
    w = tables.workers if w is None else w
    p = tables.worker_pools
    of_pool = _pool_field(w, func.array_append(p.c.labels, literal(f"{POOL_LABEL_KEY}=") + p.c.name))
    return func.coalesce(of_pool, literal([], ARRAY(Text)))


def all_labels(w=None):
    """All the labels of a worker row: its own, then its pool's (``node_labels``)."""
    w = tables.workers if w is None else w
    return func.array_cat(w.c.labels, node_labels(w))


def pool_name(w=None):
    """The name of the pool of a worker row, NULL for a worker of no pool."""
    w = tables.workers if w is None else w
    return _pool_field(w, tables.worker_pools.c.name)


def all_taints(w=None):
    """The taints of a worker row: its own, then its pool's."""
    w = tables.workers if w is None else w
    of_pool = _pool_field(w, tables.worker_pools.c.taints)
    return func.array_cat(w.c.taints, func.coalesce(of_pool, literal([], ARRAY(Text))))


def columns(w=None) -> list:
    """What ``Placement.of`` reads of a worker row, each labeled place_*, to add to a select of ``w``."""
    w = tables.workers if w is None else w
    p = tables.worker_pools
    return [
        pool_name(w).label("place_pool"),
        all_labels(w).label("place_labels"),
        all_taints(w).label("place_taints"),
        _pool_field(w, p.c.accepted_findings).label("place_accepted"),
        w.c.os.label("place_os"),
        w.c.arch.label("place_arch"),
        w.c.runtimes.label("place_runtimes"),
        w.c.features.label("place_features"),
        w.c.doctor_at.label("place_doctor_at"),
        _fresh(w.c.doctor_at).label("place_doctor_fresh"),
        w.c.doctor_findings.label("place_findings"),
    ]


def auto_labels(os: str | None, arch: str | None, runtimes: dict | None, features) -> list[str]:
    """The labels the hub gives a worker from its heartbeat: os=, arch=, runtime=<name> for each runtime it has
    (``available``), and feature=browser when its daemon starts a browser."""
    labels = [f"os={os}"] if os else []
    labels += [f"arch={arch}"] if arch else []
    labels += [f"runtime={name}" for name, report in sorted((runtimes or {}).items()) if available(report)]
    labels += [f"feature={runs.BROWSER}"] if runs.BROWSER in (features or ()) else []
    return normalized(labels)


def readiness(pool: str | None, doctor_at, fresh: bool | None, findings, accepted) -> str | None:
    """Why a node of ``pool`` takes no run now, or None when it is ready; a worker of no pool always is."""
    if pool is None:
        return None
    hours = runs.DOCTOR_FRESH_SECONDS // 3600
    if doctor_at is None:
        return (
            "it has sent no doctor report: run `evo-agents worker doctor` on it, or update its daemon to a release "
            "that sends one (feature doctor_report)"
        )
    if not fresh:
        return (
            f"its newest doctor report is older than {hours} hours: its daemon sends one when it starts and every day"
        )
    allowed = {code.lower() for code in accepted or ()}
    high = {str(f.get("code")) for f in findings or () if isinstance(f, dict) and f.get("severity") == "high"}
    high = sorted(code for code in high if code.lower() not in allowed)
    if high:
        return (
            f"its doctor found {', '.join(high)} (high), which pool {pool} does not accept: fix it and run `evo-agents "
            "worker doctor`, or add the code to the pool's accepted_findings"
        )
    return None


@dataclass(frozen=True)
class Placement:
    """What places runs on one worker: its labels, its taints, and why it takes no run now (None when ready)."""

    labels: tuple[str, ...]
    taints: tuple[str, ...]
    auto: tuple[str, ...]
    unready: str | None

    @property
    def ready(self) -> bool:
        return self.unready is None

    @classmethod
    def of(cls, row) -> Placement:
        """From a row with ``columns``."""
        m = row._mapping
        auto = tuple(auto_labels(m["place_os"], m["place_arch"], m["place_runtimes"], m["place_features"]))
        reason = readiness(
            m["place_pool"], m["place_doctor_at"], m["place_doctor_fresh"], m["place_findings"], m["place_accepted"]
        )
        labels = tuple(normalized([*(m["place_labels"] or ()), *auto]))
        return cls(labels=labels, taints=tuple(normalized(m["place_taints"])), auto=auto, unready=reason)

    def missing(self, selector) -> list[str]:
        """The labels of ``selector`` the worker lacks."""
        return [label for label in normalized(selector) if label not in self.labels]

    def untolerated(self, tolerations, pinned: bool = False) -> list[str]:
        """The taints of the worker a run tolerating ``tolerations`` does not; none for a run pinned to it."""
        if pinned:
            return []
        tolerated = set(normalized(tolerations))
        return [taint for taint in self.taints if taint not in tolerated]

    def problems(self, selector=(), tolerations=(), *, pinned: bool = False, readiness: bool = True) -> list[str]:
        """Why the worker takes no run with ``selector`` and ``tolerations``, as the claim counts it; empty when it
        does. ``readiness`` false leaves out whether it is ready now (the worker axis of a profile at dispatch)."""
        found = [f"is not ready: {self.unready}"] if readiness and self.unready else []
        if lacks := self.missing(selector):
            found.append(f"lacks the label{'s' if len(lacks) > 1 else ''} {', '.join(lacks)} the run asks")
        if taints := self.untolerated(tolerations, pinned):
            plural = "s" if len(taints) > 1 else ""
            found.append(f"has the taint{plural} {', '.join(taints)}, which the run does not tolerate (--tolerate)")
        return found

    def gate(self, worker_id: int):
        """The condition on tables.runs that a queued run fits this worker: its selector within the worker's labels
        and, when the worker has taints, the run pinned to it or tolerating each."""
        r = tables.runs
        clause = r.c.selector.contained_by(literal(list(self.labels), ARRAY(Text)))
        if not self.taints:
            return clause
        tolerated = r.c.tolerations.contains(literal(list(self.taints), ARRAY(Text)))
        return and_(clause, or_(r.c.pinned_worker_id == worker_id, tolerated))


async def placement(conn: AsyncConnection, worker_id: int) -> Placement:
    """The placement of the worker ``worker_id``."""
    w = tables.workers
    row = (await conn.execute(select(*columns(w)).where(w.c.id == worker_id))).one()
    return Placement.of(row)
