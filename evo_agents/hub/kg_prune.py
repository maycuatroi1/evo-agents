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

from evo_agents.hub import blob_gc
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import DEFAULT_KG_KEEP_ARTIFACTS, MAX_KG_KEEP_ARTIFACTS
from evo_agents.hub.kg_build import ARTIFACT_KIND

log = logging.getLogger(__name__)

DEFAULT_KEEP = DEFAULT_KG_KEEP_ARTIFACTS
MAX_KEEP = MAX_KG_KEEP_ARTIFACTS

# A project's artifacts, newest first (rank 1 is the one its reads use), then its kg-graph blobs no build points at.
PLAN = """
WITH artifacts AS (
    SELECT project_id, artifact_sha256 AS sha256, max(artifact_size) AS size, max(id) AS newest
      FROM kg_builds
     WHERE artifact_sha256 IS NOT NULL AND (%(project_id)s::bigint IS NULL OR project_id = %(project_id)s)
     GROUP BY project_id, artifact_sha256
)
SELECT p.name, a.project_id, a.sha256, a.size,
       row_number() OVER (PARTITION BY a.project_id ORDER BY a.newest DESC) AS rank
  FROM artifacts a JOIN projects p ON p.id = a.project_id
UNION ALL
SELECT p.name, b.project_id, b.sha256, b.size, NULL
  FROM blobs b JOIN projects p ON p.id = b.project_id
 WHERE b.kind = 'kg-graph' AND (%(project_id)s::bigint IS NULL OR b.project_id = %(project_id)s)
   AND NOT EXISTS (SELECT 1 FROM kg_builds k WHERE k.project_id = b.project_id AND k.artifact_sha256 = b.sha256)
ORDER BY 1, 5 NULLS LAST
"""
DROP_FROM_BUILDS = """
UPDATE kg_builds SET artifact_sha256 = NULL, artifact_pruned_at = now()
 WHERE project_id = %s AND artifact_sha256 = ANY(%s)
"""
DROP_BLOBS = "DELETE FROM blobs WHERE project_id = %s AND kind = 'kg-graph' AND sha256 = ANY(%s)"


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
    conn, keep: int, project: str | None, actor_id: int | None, token_id: int | None, audit: bool
) -> list[ProjectPruned]:
    """Drop the references to the artifacts beyond ``keep`` and record the blobs left unreferenced, in ``conn``'s
    transaction, which holds the blob lock exclusively."""
    from evo_agents.hub.server import audit as audit_trail

    project_id = None
    if project is not None:
        row = await (await conn.execute("SELECT id FROM projects WHERE name = %s", (project,))).fetchone()
        if row is None:
            raise UnknownProject(f"project {project} is not registered on this hub")
        project_id = row[0]
    rows = await (await conn.execute(PLAN, {"project_id": project_id})).fetchall()
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
        cursor = await conn.execute(DROP_FROM_BUILDS, (pid, sorted(hashes)))
        found[name].builds = cursor.rowcount
        await conn.execute(DROP_BLOBS, (pid, sorted(hashes)))
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
    pool,
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
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=dry_run):
            await blob_gc.lock_exclusive(conn)
            projects = await _drop(conn, keep, project, actor_id, token_id, audit=not dry_run)
            waiting, waiting_bytes = await blob_gc.pending(conn)
    report = PruneReport(dry_run, keep, projects)
    if dry_run:
        report.deleted, report.deleted_bytes = waiting, waiting_bytes
        return report
    deleted = await blob_gc.delete_pending(pool, store)
    report.deleted, report.deleted_bytes = deleted.objects, deleted.bytes
    async with pool.connection() as conn:
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
