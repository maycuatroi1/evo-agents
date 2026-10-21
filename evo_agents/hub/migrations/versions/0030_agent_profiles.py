"""Agent profiles: who an agent of a project is and what its runs may use, a default profile for every project, and the
profile each run and lease was made under.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-14

``evo_agents.hub.profiles`` describes the design; the patterns and bounds below are copies of its constants as of
this revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation
on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the tables.

agent_profiles holds each profile as it is now: its project, a handle unique on the hub (a project's name for its
default profile), name (1 to 80 characters), description (at most 1000), instructions (at most 8000), colour
(``#rrggbb``), the SHA-256 of its avatar, whether it is the project's default (at most one per project, with every
permission and no instructions), its permissions on the seven axes as a JSON object, its version, who created it and
when, when it changed last and when it was soft-deleted. agent_profile_versions holds the profile as each version had
it. runs and credential_leases get profile_id and profile_version, both NULL or both set, pointing at a version.

Every project gets its default profile here, created by the member who created the project, at version 1, with its
version row: its handle and name are the project's name, its colour slate. Runs and leases made before this revision
keep NULL, and a run without a profile runs as before.

Going back drops the index, the columns and the two tables, which is the schema that schema 0029 runs on; the
profiles, and which profile each run had, are lost.
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

AXES = ("credential", "tool", "skill", "worker", "project", "model", "runtime")  # evo_agents.hub.profiles.AXES
ALL_ACCESS = {axis: {"mode": "all"} for axis in AXES}
HANDLE = "^[a-z0-9][a-z0-9-]{0,99}$"
COLOR = "^#[0-9a-f]{6}$"
SHA256 = "^[0-9a-f]{64}$"
MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 1000
MAX_INSTRUCTIONS_CHARS = 8000
DEFAULT_COLOR = "#475569"
DEFAULT_DESCRIPTION = "The project's default agent: every permission, no instructions."
HELD = ("runs", "credential_leases")  # the tables that record a profile

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _all_access() -> sa.Cast:
    return sa.cast(sa.literal(json.dumps(ALL_ACCESS, sort_keys=True)), JSONB)


def _create_tables() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("handle", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("instructions", sa.Text(), server_default="", nullable=False),
        sa.Column("color", sa.Text(), nullable=False),
        sa.Column("avatar_sha256", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("permissions", JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(col("handle").regexp_match(HANDLE), name="agent_profiles_handle_check"),
        sa.CheckConstraint(
            sa.func.char_length(col("name")).between(1, MAX_NAME_CHARS), name="agent_profiles_name_check"
        ),
        sa.CheckConstraint(
            sa.func.char_length(col("description")) <= MAX_DESCRIPTION_CHARS, name="agent_profiles_description_check"
        ),
        sa.CheckConstraint(
            sa.func.char_length(col("instructions")) <= MAX_INSTRUCTIONS_CHARS, name="agent_profiles_instructions_check"
        ),
        sa.CheckConstraint(col("color").regexp_match(COLOR), name="agent_profiles_color_check"),
        sa.CheckConstraint(col("avatar_sha256").regexp_match(SHA256), name="agent_profiles_avatar_check"),
        sa.CheckConstraint(
            sa.func.jsonb_typeof(col("permissions")) == "object", name="agent_profiles_permissions_check"
        ),
        sa.CheckConstraint(col("version") >= 1, name="agent_profiles_version_check"),
        sa.CheckConstraint(  # a default profile has every permission and no instructions
            sa.or_(~col("is_default"), sa.and_(col("instructions") == "", col("permissions") == _all_access())),
            name="agent_profiles_default_check",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT", name="agent_profiles_created_by_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT", name="agent_profiles_project_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="agent_profiles_pkey"),
        sa.UniqueConstraint("handle", name="agent_profiles_handle_key"),
    )
    op.create_index("agent_profiles_project_idx", "agent_profiles", ["project_id", "handle"])
    op.create_index(
        "agent_profiles_default_key",
        "agent_profiles",
        ["project_id"],
        unique=True,
        postgresql_where=col("is_default"),
    )
    op.create_table(
        "agent_profile_versions",
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", JSONB(), nullable=False),
        sa.Column("changed_by", sa.BigInteger(), nullable=False),
        _stamp("changed_at"),
        sa.CheckConstraint(col("version") >= 1, name="agent_profile_versions_version_check"),
        sa.CheckConstraint(sa.func.jsonb_typeof(col("body")) == "object", name="agent_profile_versions_body_check"),
        sa.ForeignKeyConstraint(
            ["changed_by"], ["users.id"], ondelete="RESTRICT", name="agent_profile_versions_changed_by_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["agent_profiles.id"], ondelete="RESTRICT", name="agent_profile_versions_profile_id_fkey"
        ),
        sa.PrimaryKeyConstraint("profile_id", "version", name="agent_profile_versions_pkey"),
    )


def _add_profile_columns() -> None:
    for name in HELD:
        op.add_column(name, sa.Column("profile_id", sa.BigInteger(), nullable=True))
        op.add_column(name, sa.Column("profile_version", sa.Integer(), nullable=True))
        op.create_check_constraint(
            f"{name}_profile_check", name, col("profile_id").is_(None) == col("profile_version").is_(None)
        )
        op.create_foreign_key(
            f"{name}_profile_fkey",
            name,
            "agent_profile_versions",
            ["profile_id", "profile_version"],
            ["profile_id", "version"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "runs_profile_idx",
        "runs",
        [col("profile_id"), sa.desc(col("id"))],
        postgresql_where=col("profile_id").is_not(None),
    )


def _seed_defaults() -> None:
    """The default profile of every project, at version 1, and its version row."""
    projects = sa.table("projects", col("id"), col("name"), col("created_by"))
    profiles = sa.table(
        "agent_profiles",
        col("id"),
        col("project_id"),
        col("handle"),
        col("name"),
        col("description"),
        col("instructions"),
        col("color"),
        col("avatar_sha256"),
        col("is_default"),
        col("permissions"),
        col("version"),
        col("created_by"),
    )
    versions = sa.table("agent_profile_versions", col("profile_id"), col("version"), col("body"), col("changed_by"))
    seeded = sa.select(
        projects.c.id,
        projects.c.name,
        projects.c.name,
        sa.literal(DEFAULT_DESCRIPTION),
        sa.literal(DEFAULT_COLOR),
        sa.true(),
        _all_access(),
        projects.c.created_by,
    )
    columns = ["project_id", "handle", "name", "description", "color", "is_default", "permissions", "created_by"]
    op.execute(profiles.insert().from_select(columns, seeded))
    body = sa.func.jsonb_build_object(
        *("handle", profiles.c.handle, "name", profiles.c.name, "description", profiles.c.description),
        *(
            "instructions",
            profiles.c.instructions,
            "color",
            profiles.c.color,
            "avatar_sha256",
            profiles.c.avatar_sha256,
        ),
        *("is_default", profiles.c.is_default, "permissions", profiles.c.permissions),
    )
    first = sa.select(profiles.c.id, profiles.c.version, body, profiles.c.created_by)
    op.execute(versions.insert().from_select(["profile_id", "version", "body", "changed_by"], first))


def upgrade() -> None:
    _create_tables()
    _add_profile_columns()
    _seed_defaults()


def downgrade() -> None:
    op.drop_index("runs_profile_idx", table_name="runs")
    for name in reversed(HELD):
        op.drop_constraint(f"{name}_profile_fkey", name, type_="foreignkey")
        op.drop_constraint(f"{name}_profile_check", name, type_="check")
        op.drop_column(name, "profile_version")
        op.drop_column(name, "profile_id")
    op.drop_table("agent_profile_versions")
    op.drop_index("agent_profiles_default_key", table_name="agent_profiles")
    op.drop_index("agent_profiles_project_idx", table_name="agent_profiles")
    op.drop_table("agent_profiles")
