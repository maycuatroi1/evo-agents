"""The project an audit row is about, so the admin area can filter the trail by project.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-04

audit.project_id is the project a row's action happened in, or NULL for actions that belong to no project (signing
in and out, revoking a token, a global skill, a personal memory). ``audit.record`` fills it from the action and the
target as they are written; this revision fills it for the rows written before it, by the same rules: the first
name of the target for grant, project, plan, kg and blob actions, the project of skill:project/<project>/<name>, and
the project of the memory a memory action names. Rows are never updated otherwise, so the update trigger is off
for that one statement only. A project referenced by audit rows cannot be deleted, as for its content.

Two indexes serve the admin area: (project_id, at) for the project filter and (action, at) for the action filter
and the list of distinct actions.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

NAMED_ACTIONS = "'^(grant|project|plan|kg|blob)[.]'"
FIRST_NAME = "'^([a-z0-9][a-z0-9-]*)(?:[/ @]|$)'"

UPGRADE = (
    "ALTER TABLE audit ADD COLUMN project_id bigint REFERENCES projects (id) ON DELETE RESTRICT",
    "CREATE INDEX audit_project_id_idx ON audit (project_id, at) WHERE project_id IS NOT NULL",
    "CREATE INDEX audit_action_idx ON audit (action, at)",
    "ALTER TABLE audit DISABLE TRIGGER audit_no_update",
    f"""
    UPDATE audit a SET project_id = p.id FROM projects p
     WHERE a.action ~ {NAMED_ACTIONS} AND p.name = substring(a.target FROM {FIRST_NAME})
    """,
    """
    UPDATE audit a SET project_id = p.id FROM projects p
     WHERE a.action ~ '^skill[.]' AND p.name = substring(a.target FROM '^skill[:]project/([a-z0-9][a-z0-9-]*)/')
    """,
    """
    UPDATE audit a SET project_id = m.project_id FROM memories m
     WHERE a.action ~ '^memory[.]' AND a.target ~ '^memory[:][0-9]{1,18}$'
       AND m.id = substring(a.target FROM '([0-9]+)$')::bigint
    """,
    "ALTER TABLE audit ENABLE TRIGGER audit_no_update",
)

DOWNGRADE = (
    "DROP INDEX audit_action_idx",
    "DROP INDEX audit_project_id_idx",
    "ALTER TABLE audit DROP COLUMN project_id",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
