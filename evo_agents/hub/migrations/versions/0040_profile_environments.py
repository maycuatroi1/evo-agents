"""Environments: the eighth axis of an agent profile, and the environment of a plugin a run's agent runs in.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-18

The names below are copies of ``evo_agents.hub.profiles`` and ``evo_agents.plugins.run_environments`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the tables.

An agent profile has an eighth axis, environment: ``all``, which runs its runs on the worker's own machine (the host)
as every run ran before this revision, or an allowlist of ``host`` and environments of plugins (``<plugin>/<name>``,
a container image a plugin declares), in order of preference. Every profile gets the axis with mode all in
agent_profiles.permissions, and the check of a default profile's permissions becomes the eight axes, all. The rows of
agent_profile_versions stay as each version had them: a version from before this revision names seven axes, and the
hub reads an axis a version does not name as all (``evo_agents.hub.profiles``).

runs.environment is the environment of a plugin the claim picked for the run from its profile's axis, ``<plugin>/
<name>``; NULL for a run on the host, which every run before this revision is.

Going back drops runs.environment, takes the axis out of agent_profiles.permissions and puts the check of seven axes
back, which is the schema that schema 0039 runs on: a profile whose axis was an allowlist runs on the host again. The
versions written since keep the axis in their bodies, which schema 0039 does not read.
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

AXES_0030 = ("credential", "tool", "skill", "worker", "project", "model", "runtime")
AXES = (*AXES_0030, "environment")  # evo_agents.hub.profiles.AXES
AXIS = "environment"
PLUGIN_ENVIRONMENT = "^[a-z][a-z0-9-]{0,62}/[a-z][a-z0-9_-]{0,62}$"  # evo_agents.plugins.run_environments
DEFAULT_CHECK = "agent_profiles_default_check"

col = sa.column


def _all_access(axes: tuple[str, ...]) -> sa.Cast:
    return sa.cast(sa.literal(json.dumps({axis: {"mode": "all"} for axis in axes}, sort_keys=True)), JSONB)


def _default_check(axes: tuple[str, ...]) -> None:
    op.create_check_constraint(  # a default profile has every permission and no instructions
        DEFAULT_CHECK,
        "agent_profiles",
        sa.or_(~col("is_default"), sa.and_(col("instructions") == "", col("permissions") == _all_access(axes))),
    )


def upgrade() -> None:
    op.drop_constraint(DEFAULT_CHECK, "agent_profiles", type_="check")
    profiles = sa.table("agent_profiles", col("permissions", JSONB))
    axis = sa.cast(sa.literal(json.dumps({AXIS: {"mode": "all"}})), JSONB)
    op.execute(
        profiles.update()
        .where(~profiles.c.permissions.has_key(AXIS))
        .values(permissions=profiles.c.permissions.concat(axis))
    )
    _default_check(AXES)
    op.add_column("runs", sa.Column("environment", sa.Text(), nullable=True))
    op.create_check_constraint("runs_environment_check", "runs", col("environment").regexp_match(PLUGIN_ENVIRONMENT))


def downgrade() -> None:
    op.drop_constraint("runs_environment_check", "runs", type_="check")
    op.drop_column("runs", "environment")
    op.drop_constraint(DEFAULT_CHECK, "agent_profiles", type_="check")
    profiles = sa.table("agent_profiles", col("permissions", JSONB))
    op.execute(
        profiles.update()
        .where(profiles.c.permissions.has_key(AXIS))
        .values(permissions=profiles.c.permissions.op("-")(sa.literal(AXIS)))
    )
    _default_check(AXES_0030)
