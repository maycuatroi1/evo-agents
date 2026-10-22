"""The memories of agent profiles, and where each memory revision came from.

Revision ID: 0101
Revises: 0100
Create Date: 2026-10-22

memories.profile_id is a memory's namespace: NULL for every memory there is (the project's shared memories and a
person's own), else the agent profile whose memory it is, in a project (plan agent-profile-workspace). The keys take it
in: memories_shared_key and memories_owned_key are made again over the memories of no agent alone, which every row
there is satisfies, and memories_agent_key gives one memory per profile, location and name. A CHECK keeps an agent's
memory in a project.

memory_revisions takes the provenance of a revision the agent of a run wrote: run_id, profile_id and profile_version,
origin_id (the person at the run's origin), hop, chat_id and chat_message_id, all NULL for every revision there is and
for a write from no run. memory_revisions_run_idx finds what one run wrote.

The names below are copies of ``evo_agents.hub.tables_memory_agents`` as of this revision, never imports, so a later
change to it takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Going back deletes the memories of agents with their revisions, makes the two keys again as 0001 made them and drops
the columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

SHARED = ("project", "reference")
OWNED = ("user", "feedback")
PROVENANCE = (
    ("run_id", sa.BigInteger()),
    ("profile_id", sa.BigInteger()),
    ("profile_version", sa.Integer()),
    ("origin_id", sa.BigInteger()),
    ("hop", sa.SmallInteger()),
    ("chat_id", sa.BigInteger()),
    ("chat_message_id", sa.BigInteger()),
)

col = sa.column


def _keys(*, agents: bool) -> None:
    """memories_shared_key and memories_owned_key, over the memories of no agent when ``agents``, else as 0001."""
    where = col("scope") == "project"
    if agents:
        where = sa.and_(where, col("profile_id").is_(None))
    op.create_index(
        "memories_shared_key",
        "memories",
        ["project_id", "location", "name"],
        unique=True,
        postgresql_where=sa.and_(where, col("type").in_(SHARED)),
    )
    op.create_index(
        "memories_owned_key",
        "memories",
        ["owner_id", "project_id", "location", "name"],
        unique=True,
        postgresql_where=sa.and_(where, col("type").in_(OWNED)),
    )


def _drop_keys() -> None:
    op.drop_index("memories_owned_key", table_name="memories")
    op.drop_index("memories_shared_key", table_name="memories")


def upgrade() -> None:
    op.add_column("memories", sa.Column("profile_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "memories_profile_id_fkey", "memories", "agent_profiles", ["profile_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_check_constraint(
        "memories_agent_scope", "memories", sa.or_(col("profile_id").is_(None), col("scope") == "project")
    )
    _drop_keys()
    _keys(agents=True)
    op.create_index(
        "memories_agent_key",
        "memories",
        ["profile_id", "location", "name"],
        unique=True,
        postgresql_where=col("profile_id").is_not(None),
    )
    for name, kind in PROVENANCE:
        op.add_column("memory_revisions", sa.Column(name, kind, nullable=True))
    op.create_foreign_key(
        "memory_revisions_profile_fkey",
        "memory_revisions",
        "agent_profile_versions",
        ["profile_id", "profile_version"],
        ["profile_id", "version"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "memory_revisions_origin_id_fkey", "memory_revisions", "users", ["origin_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index(
        "memory_revisions_run_idx",
        "memory_revisions",
        ["run_id", "memory_id"],
        postgresql_where=col("run_id").is_not(None),
    )


def downgrade() -> None:
    op.drop_index("memory_revisions_run_idx", table_name="memory_revisions")
    op.drop_constraint("memory_revisions_origin_id_fkey", "memory_revisions", type_="foreignkey")
    op.drop_constraint("memory_revisions_profile_fkey", "memory_revisions", type_="foreignkey")
    for name, _ in reversed(PROVENANCE):
        op.drop_column("memory_revisions", name)
    memories = sa.table("memories", col("profile_id"))
    op.execute(sa.delete(memories).where(memories.c.profile_id.is_not(None)))  # their revisions go by the cascade
    op.drop_index("memories_agent_key", table_name="memories")
    _drop_keys()
    _keys(agents=False)
    op.drop_constraint("memories_agent_scope", "memories", type_="check")
    op.drop_constraint("memories_profile_id_fkey", "memories", type_="foreignkey")
    op.drop_column("memories", "profile_id")
