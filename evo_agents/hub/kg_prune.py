"""The retention of built graphs: a project keeps the artifacts of its latest graphs, the others leave the bucket.

A build whose content hash equals that of its project's latest build still holding an artifact points at that
artifact instead of uploading another (``evo_agents.hub.kg_build``), so a new artifact means a new graph. ``prune``
keeps, per project, the artifacts of its ``keep`` newest graphs: the distinct artifacts its latest succeeded builds
point at, one shared by several builds counted once, so the newest, which the api reads, always stays. Every other
artifact of the project goes, together with any kg-graph blob of the project no build points at. In one transaction
holding the blob lock exclusively (``evo_agents.hub.blob_gc``), the builds pointing at it get ``artifact_pruned_at``
and lose ``artifact_sha256``, its ``blobs`` row (kind kg-graph) is deleted, and ``blob_gc.forget`` records it for
deletion when nothing else refers to it. ``blob_gc.delete_pending`` then deletes the objects.

Only kg-graph rows and build artifacts are dropped: run logs, source blobs and skill bundles are never touched, and
an object any other row refers to stays. A queued or running build holds no artifact yet; one that finishes while a
prune runs records its artifact holding the blob lock shared, so one waits for the other, and the artifact it reuses
is its project's newest, which a prune keeps. A second run right after the first finds nothing to drop.

The worker prunes every hour (``hub.prune_kg_artifacts``) with EVO_HUB_KG_KEEP_ARTIFACTS (default 3). An admin prunes
by hand with ``evo-agents hub kg prune`` (POST /v1/admin/kg/prune), and ``--dry-run`` answers the same report from a
transaction that is rolled back.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from sqlalchemy import delete, exists, func, null, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import DEFAULT_KG_KEEP_ARTIFACTS, MAX_KG_KEEP_ARTIFACTS
from evo_agents.hub.kg_build import ARTIFACT_KIND

log = logging.getLogger(__name__)

DEFAULT_KEEP = DEFAULT_KG_KEEP_ARTIFACTS
MAX_KEEP = MAX_KG_KEEP_ARTIFACTS


def _plan(project_id: int | None):
    """The artifacts of each project (only ``project_id``'s when given), newest first: rank 1 is the one its reads
    use. Then its kg-graph blobs no build points at, with no rank. Rows of (name, project_id, sha256, size, rank),
    by project name."""
    builds, blobs, projects = tables.kg_builds, tables.blobs, tables.projects
    artifacts = select(
        builds.c.project_id,
        builds.c.artifact_sha256.label("sha256"),
        func.max(builds.c.artifact_size).label("size"),
        func.max(builds.c.id).label("newest"),
    ).where(builds.c.artifact_sha256.is_not(None))
    unreferenced = (
        select(projects.c.name, blobs.c.project_id, blobs.c.sha256, blobs.c.size, null())
        .join_from(blobs, projects, projects.c.id == blobs.c.project_id)
        .where(
            blobs.c.kind == ARTIFACT_KIND,
            ~exists().where(builds.c.project_id == blobs.c.project_id, builds.c.artifact_sha256 == blobs.c.sha256),
        )
    )
    if project_id is not None:
        artifacts = artifacts.where(builds.c.project_id == project_id)
        unreferenced = unreferenced.where(blobs.c.project_id == project_id)
    artifacts = artifacts.group_by(builds.c.project_id, builds.c.artifact_sha256).cte("artifacts")
    rank = func.row_number().over(partition_by=artifacts.c.project_id, order_by=artifacts.c.newest.desc())
    ranked = select(
        projects.c.name, artifacts.c.project_id, artifacts.c.sha256, artifacts.c.size, rank.label("rank")
    ).join_from(artifacts, projects, projects.c.id == artifacts.c.project_id)
    plan = union_all(ranked, unreferenced)
    return plan.order_by(plan.selected_columns.name, plan.selected_columns.rank.nulls_last())


class UnknownProject(LookupError):
    """The project to prune is not registered on the hub."""


@dataclass
class ProjectPruned:
    project: str
    artifacts: int = 0  # distinct artifacts of the project before, unreferenced kg-graph blobs included
    kept: int = 0
    pruned: int = 0  # artifacts dropped
    pruned_bytes: int = 0
    builds: int = 0  # builds whose artifact was dropped


@dataclass
class PruneReport:
    dry_run: bool
    keep: int
    projects: list[ProjectPruned] = field(default_factory=list)
    deleted: int = 0  # objects deleted from the bucket by this run; for a dry run, those it would delete
    deleted_bytes: int = 0
    pending: int = 0  # blobs whose object still waits to be deleted after this run, as when the bucket failed

    def to_json(self) -> dict:
        return asdict(self)

    def summary(self) -> dict:
        """What the worker's job returns: the totals, without one entry per project."""
        return {
            "dry_run": self.dry_run,
            "keep": self.keep,
            "pruned": sum(p.pruned for p in self.projects),
            "builds": sum(p.builds for p in self.projects),
            "deleted": self.deleted,
            "deleted_bytes": self.deleted_bytes,
            "pending": self.pending,
        }


def check_keep(keep: int) -> int:
    if not 1 <= keep <= MAX_KEEP:
        raise ValueError(f"keep must be between 1 and {MAX_KEEP}, not {keep}: a project always keeps its newest graph")
    return keep


async def _drop(
    conn: AsyncConnection, keep: int, project: str | None, actor_id: int | None, token_id: int | None, audit: bool
) -> list[ProjectPruned]:
    """Drop the references to the artifacts beyond ``keep`` and record the blobs left unreferenced, in ``conn``'s
    transaction, which holds the blob lock exclusively."""
    from evo_agents.hub.server import audit as audit_trail

    builds, blobs, projects = tables.kg_builds, tables.blobs, tables.projects
    project_id = None
    if project is not None:
        row = (await conn.execute(select(projects.c.id).where(projects.c.name == project))).one_or_none()
        if row is None:
            raise UnknownProject(f"project {project} is not registered on this hub")
        project_id = row[0]
    rows = (await conn.execute(_plan(project_id))).all()
    found: dict[str, ProjectPruned] = {}
    doomed: dict[tuple[int, str], dict[str, int]] = {}  # (project id, name) -> {sha256: size}
    for name, pid, sha256, size, rank in rows:
        entry = found.setdefault(name, ProjectPruned(name))
        entry.artifacts += 1
        if rank is not None and rank <= keep:
            entry.kept += 1
            continue
        entry.pruned += 1
        entry.pruned_bytes += size
        doomed.setdefault((pid, name), {})[sha256] = size
    if project is not None and project not in found:
        found[project] = ProjectPruned(project)
    left: dict[str, tuple[int, str]] = {}
    for (pid, name), hashes in doomed.items():
        dropped = await conn.execute(
            update(builds)
            .values(artifact_sha256=None, artifact_pruned_at=func.now())
            .where(builds.c.project_id == pid, builds.c.artifact_sha256.in_(sorted(hashes)))
        )
        found[name].builds = dropped.rowcount
        await conn.execute(
            delete(blobs).where(
                blobs.c.project_id == pid, blobs.c.kind == ARTIFACT_KIND, blobs.c.sha256.in_(sorted(hashes))
            )
        )
        left.update({sha: (size, ARTIFACT_KIND) for sha, size in hashes.items()})
        if audit:
            await audit_trail.record(
                conn,
                actor_id=actor_id,
                token_id=token_id,
                action=audit_trail.KG_PRUNE,
                target=f"{name} keep={keep}",
                project_id=pid,
            )
    await blob_gc.forget(conn, left)
    return [found[name] for name in sorted(found)]


async def prune(
    engine,
    store: BlobStore,
    keep: int = DEFAULT_KEEP,
    project: str | None = None,
    *,
    dry_run: bool = False,
    actor_id: int | None = None,
    token_id: int | None = None,
) -> PruneReport:
    """Keep the artifacts of the ``keep`` newest graphs of ``project`` (every project when None) and delete the
    others (see the module). ``actor_id`` and ``token_id`` name who asked, in the audit trail; None is the hub
    itself. Raises ValueError for ``keep`` below 1, UnknownProject, and BlobStoreUnavailable when the bucket does
    not delete: the builds are marked then, and the objects wait for the next run."""
    check_keep(keep)
    async with engine.connect() as conn, conn.begin() as transaction:
        await blob_gc.lock_exclusive(conn)
        projects = await _drop(conn, keep, project, actor_id, token_id, audit=not dry_run)
        waiting, waiting_bytes = await blob_gc.pending(conn)
        if dry_run:  # what a run would do, written nowhere
            await transaction.rollback()
    report = PruneReport(dry_run, keep, projects)
    if dry_run:
        report.deleted, report.deleted_bytes = waiting, waiting_bytes
        return report
    deleted = await blob_gc.delete_pending(engine, store)
    report.deleted, report.deleted_bytes = deleted.objects, deleted.bytes
    async with engine.begin() as conn:
        report.pending = (await blob_gc.pending(conn))[0]
    if any(p.pruned for p in projects):
        log.info(
            "kg artifacts pruned",
            extra={
                "keep": keep,
                "projects": {p.project: p.pruned for p in projects if p.pruned},
                "builds": sum(p.builds for p in projects),
                "deleted": report.deleted,
                "deleted_bytes": report.deleted_bytes,
                "pending": report.pending,
            },
        )
    return report
