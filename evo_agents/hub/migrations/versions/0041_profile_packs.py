"""Agent packs: the packs of plugins an agent profile attaches, each pinned at a version of its plugin.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-18

The names below are copies of ``evo_agents.hub.profiles`` and ``evo_agents.plugins.agent_packs`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions, as ``evo_agents.hub.tables`` describes the tables.

agent_profiles.packs is the list of the packs a profile attaches, in order, each ``{"plugin", "name", "version"}``:
pack ``name`` of plugin ``plugin`` at version ``version`` of the plugin (``docs/plugins.md``, section Agent packs).
Every profile gets an empty list, and the check of a default profile also holds its list empty, so a run that names
no agent runs as before. The rows of agent_profile_versions stay as each version had them: a version from before this
revision names no packs, and the hub reads it as none.

Going back drops the column and puts the check of schema 0040 back. A profile that attached packs keeps its other
fields and runs without them; the versions written since keep the packs in their bodies, which schema 0040 does not
read.
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

AXES = ("credential", "tool", "skill", "worker", "project", "model", "runtime", "environment")
DEFAULT_CHECK = "agent_profiles_default_check"
PACKS_CHECK = "agent_profiles_packs_check"
MAX_PACKS = 16  # evo_agents.hub.profiles.MAX_PACKS

col = sa.column


def _jsonb(value) -> sa.Cast:
    return sa.cast(sa.literal(json.dumps(value, sort_keys=True)), JSONB)


def _default_check(*, packs: bool) -> None:
    held = [col("instructions") == "", col("permissions") == _jsonb({axis: {"mode": "all"} for axis in AXES})]
    if packs:
        held.append(col("packs") == _jsonb([]))
    # a default profile has every permission, no instructions and, since this revision, no pack
    op.create_check_constraint(DEFAULT_CHECK, "agent_profiles", sa.or_(~col("is_default"), sa.and_(*held)))


def upgrade() -> None:
    op.add_column("agent_profiles", sa.Column("packs", JSONB(), nullable=False, server_default="[]"))
    op.create_check_constraint(
        PACKS_CHECK,
        "agent_profiles",
        sa.case(  # the length of an array only: jsonb_array_length raises on anything else
            (sa.func.jsonb_typeof(col("packs")) == "array", sa.func.jsonb_array_length(col("packs")) <= MAX_PACKS),
            else_=sa.false(),
        ),
    )
    op.drop_constraint(DEFAULT_CHECK, "agent_profiles", type_="check")
    _default_check(packs=True)


def downgrade() -> None:
    op.drop_constraint(DEFAULT_CHECK, "agent_profiles", type_="check")
    op.drop_constraint(PACKS_CHECK, "agent_profiles", type_="check")
    op.drop_column("agent_profiles", "packs")
    _default_check(packs=False)
