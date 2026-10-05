"""Built graphs stop piling up in the bucket: a build reuses the artifact of the one before when its content did not
change, the retention deletes the artifacts of older builds, and blob_deletions records the blobs being deleted.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05

kg_builds.artifact_reused_from is the build whose upload made the artifact a build points at, when the build's content
hash equalled that of the project's latest build still holding an artifact and nothing was uploaded; NULL when the
build uploaded its own. kg_builds.artifact_pruned_at is when the retention took a succeeded build's artifact away. Its
artifact_sha256 is NULL from then on, so no row names an object that is gone, while artifact_size, content_hash, nodes
and edges keep describing the graph the build made. A succeeded build has exactly one of the two.

blob_deletions holds every blob the hub decided to delete because nothing refers to it any longer (no blobs row of any
holder, skill version, build artifact or run log): its hash, size and kind, when that was decided, and when its object
was deleted (NULL until then). The row is written in the transaction that removed the last reference, and the object
is deleted afterwards (``evo_agents.hub.blob_gc``), so a deletion that fails stays pending and is tried again. Writing
a reference to a hash that has a row deletes the row, after writing the bytes again. Rows are kept for 30 days after
their object went.

Going back marks the pruned builds failed, since the older schema requires an artifact of every succeeded build.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

HEX_SHA256 = "'^[0-9a-f]{64}$'"
KIND = r"text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9-]*$' AND char_length(kind) <= 40)"
PRUNED = "the artifact of this build was deleted by the retention of built graphs"

UPGRADE = (
    "ALTER TABLE kg_builds ADD COLUMN artifact_reused_from bigint REFERENCES kg_builds (id) ON DELETE RESTRICT",
    "ALTER TABLE kg_builds ADD COLUMN artifact_pruned_at timestamptz",
    "ALTER TABLE kg_builds DROP CONSTRAINT kg_builds_check",
    """
    ALTER TABLE kg_builds ADD CONSTRAINT kg_builds_artifact_check
        CHECK ((status = 'succeeded') = (artifact_sha256 IS NOT NULL OR artifact_pruned_at IS NOT NULL))
    """,
    "ALTER TABLE kg_builds ADD CONSTRAINT kg_builds_pruned_check "
    "CHECK (artifact_pruned_at IS NULL OR artifact_sha256 IS NULL)",
    """
    ALTER TABLE kg_builds ADD CONSTRAINT kg_builds_reused_check
        CHECK (artifact_reused_from IS NULL OR (status = 'succeeded' AND artifact_reused_from <> id))
    """,
    # the graph a project's reads and the next build start from: its latest build that still holds an artifact
    "CREATE INDEX kg_builds_artifact_idx ON kg_builds (project_id, id DESC) WHERE artifact_sha256 IS NOT NULL",
    "CREATE INDEX kg_builds_artifact_sha256_idx ON kg_builds (artifact_sha256) WHERE artifact_sha256 IS NOT NULL",
    f"""
    CREATE TABLE blob_deletions (
        sha256 text PRIMARY KEY CHECK (sha256 ~ {HEX_SHA256}),
        size bigint NOT NULL CHECK (size >= 0),
        kind {KIND},
        requested_at timestamptz NOT NULL DEFAULT now(),
        deleted_at timestamptz CHECK (deleted_at >= requested_at)
    )
    """,
    "CREATE INDEX blob_deletions_pending_idx ON blob_deletions (requested_at) WHERE deleted_at IS NULL",
)

DOWNGRADE = (
    "DROP TABLE blob_deletions",
    "DROP INDEX kg_builds_artifact_sha256_idx",
    "DROP INDEX kg_builds_artifact_idx",
    "ALTER TABLE kg_builds DROP CONSTRAINT kg_builds_reused_check",
    "ALTER TABLE kg_builds DROP CONSTRAINT kg_builds_pruned_check",
    "ALTER TABLE kg_builds DROP CONSTRAINT kg_builds_artifact_check",
    f"UPDATE kg_builds SET status = 'failed', error = '{PRUNED}' WHERE artifact_pruned_at IS NOT NULL",
    "ALTER TABLE kg_builds ADD CONSTRAINT kg_builds_check CHECK ((status = 'succeeded') = "
    "(artifact_sha256 IS NOT NULL))",
    "ALTER TABLE kg_builds DROP COLUMN artifact_pruned_at",
    "ALTER TABLE kg_builds DROP COLUMN artifact_reused_from",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
