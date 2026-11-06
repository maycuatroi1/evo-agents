"""Agent systems of a project, and where an agent profile runs: agent_systems, agent_system_revisions,
agent_profiles.runs_on.

Revision ID: 0217
Revises: 0216
Create Date: 2026-11-07

An agent system (plan multi-agent-systems, step 2) is a project's agents, named graphs, curator and orchestrator on a
canvas with typed edges between them, kept by revision like a plan (``evo_agents.hub.system_spec``). A profile is the
entry agent of at most one system: a partial unique index holds it. A profile runs on a worker (``worker``, every
profile there is) or on the hub (``hub``); this revision only stores it.

Going back drops the tables and the column.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0217"
down_revision = "0216"
branch_labels = None
depends_on = None

NAME = "agent_systems"
REVISIONS = "agent_system_revisions"
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.add_column("agent_profiles", sa.Column("runs_on", sa.Text(), nullable=False, server_default="worker"))
    op.create_check_constraint("agent_profiles_runs_on_check", "agent_profiles", "runs_on in ('worker', 'hub')")
    op.create_table(
        NAME,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("entry_profile_id", sa.BigInteger()),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("updated_by", sa.BigInteger()),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{NAME}_project_id_fkey"),
        sa.ForeignKeyConstraint(
            ["entry_profile_id"], ["agent_profiles.id"], ondelete="SET NULL", name=f"{NAME}_entry_profile_id_fkey"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL", name=f"{NAME}_created_by_fkey"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL", name=f"{NAME}_updated_by_fkey"),
        sa.PrimaryKeyConstraint("id", name=f"{NAME}_pkey"),
        sa.UniqueConstraint("project_id", "slug", name=f"{NAME}_project_id_slug_key"),
        sa.CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name=f"{NAME}_slug_check"),
        sa.CheckConstraint("jsonb_typeof(spec) = 'object'", name=f"{NAME}_spec_check"),
    )
    op.create_index(
        f"{NAME}_entry_profile_key",
        NAME,
        ["entry_profile_id"],
        unique=True,
        postgresql_where=sa.column("entry_profile_id").is_not(None),
    )
    op.create_table(
        REVISIONS,
        sa.Column("system_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("actor_id", sa.BigInteger()),
        _stamp("created_at"),
        sa.ForeignKeyConstraint(["system_id"], [f"{NAME}.id"], ondelete="CASCADE", name=f"{REVISIONS}_system_id_fkey"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL", name=f"{REVISIONS}_actor_id_fkey"),
        sa.PrimaryKeyConstraint("system_id", "revision", name=f"{REVISIONS}_pkey"),
    )


def downgrade() -> None:
    op.drop_table(REVISIONS)
    op.drop_index(f"{NAME}_entry_profile_key", table_name=NAME)
    op.drop_table(NAME)
    op.drop_constraint("agent_profiles_runs_on_check", "agent_profiles", type_="check")
    op.drop_column("agent_profiles", "runs_on")
