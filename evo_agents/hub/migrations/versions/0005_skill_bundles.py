"""Skills: bundles of global skills held by the hub itself, skill names unique ignoring case, bounds on versions.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-04

A global skill belongs to no project, so neither does its bundle: blobs and blob_uploads take a NULL project for a blob
the hub holds itself, and only for kind skill-bundle (only a hub admin uploads one, see ``server/blobs.py``). The
primary key of blobs becomes a unique constraint with NULLS NOT DISTINCT (Postgres 15 and later) on the same columns,
so ``ON CONFLICT (project_id, sha256)`` keeps working and the hub holds one row per blob.

Skill names are directory names on every machine, and macOS and Windows compare those ignoring case: two skills of one
scope whose names differ only in case would land in the same directory, so the unique indexes compare lower(name).
``synced`` and ``learned`` are directories other tools keep among the skills of a runtime, never a skill. A version's
description and source repo are bounded like the API bounds them.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

GLOBAL_KINDS = "('skill-bundle')"
RESERVED = "('synced', 'learned')"  # as evo_agents.hub.skills.RESERVED_NAMES
MAX_DESCRIPTION = 4096  # characters, as evo_agents.hub.skills.MAX_DESCRIPTION
MAX_SOURCE_REPO = 200

UPGRADE = (
    "ALTER TABLE blobs DROP CONSTRAINT blobs_pkey",
    "ALTER TABLE blobs ALTER COLUMN project_id DROP NOT NULL",
    "ALTER TABLE blobs ADD CONSTRAINT blobs_holder_key UNIQUE NULLS NOT DISTINCT (project_id, sha256)",
    f"ALTER TABLE blobs ADD CONSTRAINT blobs_hub_kind CHECK (project_id IS NOT NULL OR kind IN {GLOBAL_KINDS})",
    "ALTER TABLE blob_uploads ALTER COLUMN project_id DROP NOT NULL",
    f"""
    ALTER TABLE blob_uploads ADD CONSTRAINT blob_uploads_hub_kind
        CHECK (project_id IS NOT NULL OR kind IN {GLOBAL_KINDS})
    """,
    "DROP INDEX skills_global_key",
    "DROP INDEX skills_project_key",
    "CREATE UNIQUE INDEX skills_global_key ON skills (lower(name)) WHERE scope = 'global'",
    "CREATE UNIQUE INDEX skills_project_key ON skills (project_id, lower(name)) WHERE scope = 'project'",
    f"ALTER TABLE skills ADD CONSTRAINT skills_name_reserved CHECK (lower(name) NOT IN {RESERVED})",
    f"""
    ALTER TABLE skill_versions
        ADD CONSTRAINT skill_versions_bounds CHECK (
            char_length(name) <= 100 AND char_length(description) <= {MAX_DESCRIPTION}
            AND char_length(source_repo) <= {MAX_SOURCE_REPO})
    """,
)

# Going back drops what the old schema cannot hold: the hub's own blobs and uploads. The versions of global skills
# keep their key, and the bundles stay in the bucket.
DOWNGRADE = (
    "ALTER TABLE skill_versions DROP CONSTRAINT skill_versions_bounds",
    "ALTER TABLE skills DROP CONSTRAINT skills_name_reserved",
    "DROP INDEX skills_project_key",
    "DROP INDEX skills_global_key",
    "CREATE UNIQUE INDEX skills_global_key ON skills (name) WHERE scope = 'global'",
    "CREATE UNIQUE INDEX skills_project_key ON skills (project_id, name) WHERE scope = 'project'",
    "ALTER TABLE blob_uploads DROP CONSTRAINT blob_uploads_hub_kind",
    "DELETE FROM blob_uploads WHERE project_id IS NULL",
    "ALTER TABLE blob_uploads ALTER COLUMN project_id SET NOT NULL",
    "ALTER TABLE blobs DROP CONSTRAINT blobs_hub_kind",
    "DELETE FROM blobs WHERE project_id IS NULL",
    "ALTER TABLE blobs DROP CONSTRAINT blobs_holder_key",
    "ALTER TABLE blobs ALTER COLUMN project_id SET NOT NULL",
    "ALTER TABLE blobs ADD PRIMARY KEY (project_id, sha256)",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
