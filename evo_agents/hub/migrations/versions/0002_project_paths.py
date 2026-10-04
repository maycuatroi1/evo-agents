"""Where a project's harness and repos sit relative to a workspace, so any machine can find them.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-04

``hub project register`` sends the harness.yaml name of the cluster, the workspace (``~/github`` rather than one
machine's home directory when it is under the home), and the paths of the harness root and of every repo relative
to that workspace. ``hub registry pull`` turns them back into absolute paths on the machine it runs on. The
columns are NULL for a project registered without them, which ``hub registry pull`` skips.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

PATH = r"text CHECK (char_length({0}) BETWEEN 1 AND 4096 AND {0} !~ '[\x01-\x1f\x7f]')"  # no control characters

UPGRADE = (
    f"""
    ALTER TABLE projects
        ADD COLUMN cluster text CHECK (cluster ~ '^[a-z0-9][a-z0-9-]*$' AND char_length(cluster) <= 100),
        ADD COLUMN workspace {PATH.format("workspace")},
        ADD COLUMN harness_path {PATH.format("harness_path")},
        ADD CONSTRAINT projects_harness_check CHECK ((cluster IS NULL) = (workspace IS NULL)
                                                     AND (cluster IS NULL) = (harness_path IS NULL))
    """,
    f"ALTER TABLE project_repos ADD COLUMN path {PATH.format('path')}",
)

DOWNGRADE = (
    "ALTER TABLE project_repos DROP COLUMN path",
    "ALTER TABLE projects DROP CONSTRAINT projects_harness_check, DROP COLUMN harness_path, DROP COLUMN workspace, "
    "DROP COLUMN cluster",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
