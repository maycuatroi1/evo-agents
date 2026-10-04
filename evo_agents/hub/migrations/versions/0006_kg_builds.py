"""Knowledge graphs on the hub: the knowledge config a project's graph is built with, the runs pushed but not yet
committed, and the builds.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-04

kg_configs holds, per project, what ``evo-agents hub kg push`` sent of its knowledge.yaml: the policy, the
identifiers, each source's id, connector, label and code backend, and the ontology extension, as the build needs
them; never a source's config, command or credential. digest is the SHA-256 of its canonical JSON, and each build
names the digest it was built with.

kg_pending_runs is a run whose log the hub checked and committed (blobs/sha256/<log_sha256>) but whose blobs may
not all be there yet; blobs lists the hex SHA-256 of every blob the log refers to. Committing the run moves it to
kg_ingests (schema 0001) in one transaction with the build job it queues, and deletes this row.

kg_builds is one build of a project's graph: queued when its procrastinate job is deferred (job_id; no foreign key,
finished jobs are pruned after 14 days), running, then succeeded with the SQLite artifact at
blobs/sha256/<artifact_sha256> and its content hash, or failed with an error that names no content.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

HEX_SHA256 = "'^[0-9a-f]{64}$'"
PREFIXED_SHA256 = "'^sha256:[0-9a-f]{64}$'"  # the kg/1 form of a digest
STATUSES = "('queued', 'running', 'succeeded', 'failed')"

UPGRADE = (
    f"""
    CREATE TABLE kg_configs (
        project_id bigint PRIMARY KEY REFERENCES projects (id) ON DELETE RESTRICT,
        digest text NOT NULL CHECK (digest ~ {PREFIXED_SHA256}),
        knowledge jsonb NOT NULL CHECK (jsonb_typeof(knowledge) = 'object'),
        ontology jsonb CHECK (jsonb_typeof(ontology) = 'object'),
        updated_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE kg_pending_runs (
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        run_id uuid NOT NULL,
        source text NOT NULL CHECK (source <> ''),
        log_sha256 text NOT NULL CHECK (log_sha256 ~ {HEX_SHA256}),
        log_size bigint NOT NULL CHECK (log_size > 0),
        blobs text[] NOT NULL,
        pushed_by bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (project_id, run_id)
    )
    """,
    f"""
    CREATE TABLE kg_builds (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
        job_id bigint UNIQUE,
        status text NOT NULL CHECK (status IN {STATUSES}),
        requested_by bigint REFERENCES users (id) ON DELETE RESTRICT,  -- NULL when an ingest queued it
        config_digest text CHECK (config_digest ~ {PREFIXED_SHA256}),
        runs integer CHECK (runs >= 0),
        artifact_sha256 text CHECK (artifact_sha256 ~ {HEX_SHA256}),
        artifact_size bigint CHECK (artifact_size > 0),
        content_hash text CHECK (content_hash ~ {PREFIXED_SHA256}),
        nodes integer CHECK (nodes >= 0),
        edges integer CHECK (edges >= 0),
        error text CHECK (char_length(error) BETWEEN 1 AND 2000),
        queued_at timestamptz NOT NULL DEFAULT now(),
        started_at timestamptz,
        finished_at timestamptz,
        CHECK ((status = 'succeeded') = (artifact_sha256 IS NOT NULL)),
        CHECK (status <> 'succeeded' OR (artifact_size IS NOT NULL AND content_hash IS NOT NULL
                                         AND nodes IS NOT NULL AND edges IS NOT NULL)),
        CHECK ((status = 'failed') = (error IS NOT NULL)),
        CHECK ((status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)),
        CHECK (status = 'queued' OR started_at IS NOT NULL)
    )
    """,
    "CREATE INDEX kg_builds_project_idx ON kg_builds (project_id, id DESC)",
    # the graph the api answers from: a project's latest successful build
    "CREATE INDEX kg_builds_succeeded_idx ON kg_builds (project_id, id DESC) WHERE status = 'succeeded'",
)

DOWNGRADE = ("DROP TABLE kg_builds, kg_pending_runs, kg_configs",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
