"""Blobs in the R2 bucket, the uploads waiting to be committed, and procrastinate's job queue.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-04

blobs says which project holds which blob, keyed by (project, sha256): a project reaches only the blobs it uploaded
and the hub verified, whoever else holds the same bytes. The bytes are at blobs/sha256/<sha256> in the bucket, one
object shared by every project holding them. created_by is NULL for a blob the hub made itself, such as a built kg.
blob_uploads lists the uploads the hub issued a presigned PUT for and that are not committed yet; the worker removes
rows older than 24 hours together with their objects.

procrastinate's tables, types and functions come from the SQL of the release the hub-server extra pins
(PROCRASTINATE_VERSION), vendored next to this file in ../sql/ with its MIT license, so a fresh database gets the
schema the code was tested with whatever release is installed. Upgrading procrastinate takes a new revision that
runs the files of procrastinate/sql/migrations between the two releases, in order, the pre ones and the post ones
together (the api and the worker migrate before they start, so no old code runs against the new schema), the new
pin in pyproject.toml, and the new release's schema.sql vendored for tests/hub/test_worker.py to compare with.

The hub's own statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word. The
procrastinate SQL holds both and several statements in one string, so it goes to psycopg without parameters, on the
same connection and in the same transaction.
"""

from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

PROCRASTINATE_VERSION = "3.10.0"
PROCRASTINATE_SQL = Path(__file__).parents[1] / "sql" / f"procrastinate-{PROCRASTINATE_VERSION}.sql"
HEX_SHA256 = "'^[0-9a-f]{64}$'"
KIND = r"text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9-]*$' AND char_length(kind) <= 40)"

UPGRADE = (
    f"""
    CREATE TABLE blobs (
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
        sha256 text NOT NULL CHECK (sha256 ~ {HEX_SHA256}),
        size bigint NOT NULL CHECK (size >= 0),
        kind {KIND},
        created_by bigint REFERENCES users (id) ON DELETE RESTRICT,
        verified_at timestamptz NOT NULL DEFAULT now(),  -- when the hub read the bytes back and hashed them
        PRIMARY KEY (project_id, sha256)
    )
    """,
    "CREATE INDEX blobs_sha256_idx ON blobs (sha256)",  # which projects hold a blob, for a later sweep of the bucket
    f"""
    CREATE TABLE blob_uploads (
        upload_id uuid PRIMARY KEY,  -- the object is at uploads/<upload_id>
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        sha256 text NOT NULL CHECK (sha256 ~ {HEX_SHA256}),
        size bigint NOT NULL CHECK (size >= 0),
        kind {KIND},
        created_by bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX blob_uploads_created_at_idx ON blob_uploads (created_at)",
)

# procrastinate's schema creates tables with their triggers and sequences, functions and types; its functions return
# the tables' row types and its triggers call its functions, so they go in this order.
DROP_PROCRASTINATE = r"""
DO $$
DECLARE
    found record;
BEGIN
    FOR found IN
        SELECT t.tgname, t.tgrelid::regclass AS tab FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
         WHERE NOT t.tgisinternal AND c.relnamespace = current_schema()::regnamespace
           AND c.relname LIKE 'procrastinate\_%'
    LOOP
        EXECUTE format('DROP TRIGGER %I ON %s', found.tgname, found.tab);
    END LOOP;
    FOR found IN
        SELECT oid::regprocedure AS proc FROM pg_proc
         WHERE pronamespace = current_schema()::regnamespace AND proname LIKE 'procrastinate\_%'
    LOOP
        EXECUTE 'DROP FUNCTION ' || found.proc;
    END LOOP;
END
$$;
DROP TABLE procrastinate_events, procrastinate_periodic_defers, procrastinate_jobs, procrastinate_workers;
DROP TYPE procrastinate_job_to_defer_v1, procrastinate_job_event_type, procrastinate_job_status;
"""


def _psycopg():
    """The psycopg connection under Alembic's, inside the migration's transaction."""
    return op.get_bind().connection.driver_connection


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)
    _psycopg().execute(PROCRASTINATE_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    _psycopg().execute(DROP_PROCRASTINATE)
    op.execute("DROP TABLE blob_uploads, blobs")
