"""The org chart: org_units, org_seats and autonomy_ceilings.

Revision ID: 0099
Revises: 0098
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.org`` and ``evo_agents.hub.tables_org`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

org_units is a tree with one root (plan org-chart, criterion a1): the root alone has no parent and is of kind org, which
a unique index keeps to one row; a team or a cohort has a parent; only a cohort has an end date. org_seats holds the
seats of people and agent profiles: exactly one of the two, role agent exactly for a profile, which always has a
sponsor and is primary; an intern's seat reports to a seat; a person holds at most one live primary seat and a
profile at most one live seat (two unique indexes). autonomy_ceilings holds the revisions of the ceiling of a unit or
a seat, A0 to A5. That the tree has no cycle, that reports_to names a live seat of another person and who may write
what are the service's (``evo_agents.hub.server.org_store``), under one lock.

The revision writes the root (slug root, kind org) with a ceiling of A5, and a primary seat of role member at the root
for every user there is, so the hub reads as before: every effective ceiling is A5.

Going back drops the three tables; the chart is lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

UNIT_KINDS = ("org", "team", "cohort")
ROLES = ("lead", "member", "intern", "agent")
SCOPES = ("unit", "seat")
LEVELS = ("A0", "A1", "A2", "A3", "A4", "A5")
SLUG = "^[a-z0-9][a-z0-9-]{0,62}$"
MAX_NAME_CHARS = 200
MAX_NOTE_CHARS = 1000
ROOT_SLUG, ROOT_NAME, ROOT_LEVEL = "root", "Organization", "A5"

col = sa.column


def _key(table: str, column: str, target: str, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _units() -> None:
    op.create_table(
        "org_units",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        _key("org_units", "parent_id", "org_units", "RESTRICT"),
        _key("org_units", "created_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="org_units_pkey"),
        sa.UniqueConstraint("slug", name="org_units_slug_key"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(UNIT_KINDS),
                col("parent_id").is_(None) == (col("kind") == "org"),
                sa.or_(col("ends_on").is_(None), col("kind") == "cohort"),
                col("slug").regexp_match(SLUG),
                sa.func.char_length(col("name")).between(1, MAX_NAME_CHARS),
                col("revision") >= 1,
                sa.or_(col("parent_id").is_(None), col("parent_id") != col("id")),
            ),
            name="org_units_check",
        ),
    )
    op.create_index("org_units_parent_idx", "org_units", ["parent_id"])
    op.create_index(
        "org_units_one_root_key", "org_units", ["kind"], unique=True, postgresql_where=col("parent_id").is_(None)
    )


def _seats() -> None:
    op.create_table(
        "org_seats",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("unit_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("reports_to", sa.BigInteger(), nullable=True),
        sa.Column("sponsor_id", sa.BigInteger(), nullable=True),
        sa.Column("starts_on", sa.Date(), server_default=sa.func.current_date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        _key("org_seats", "unit_id", "org_units", "RESTRICT"),
        _key("org_seats", "user_id", "users", "CASCADE"),
        _key("org_seats", "profile_id", "agent_profiles", "CASCADE"),
        _key("org_seats", "reports_to", "org_seats", "RESTRICT"),
        _key("org_seats", "sponsor_id", "users", "RESTRICT"),
        _key("org_seats", "created_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="org_seats_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("role").in_(ROLES),
                col("user_id").is_(None) != col("profile_id").is_(None),
                (col("role") == "agent") == col("profile_id").is_not(None),
                sa.or_(col("profile_id").is_(None), sa.and_(col("sponsor_id").is_not(None), col("is_primary"))),
                sa.or_(col("role") != "intern", col("reports_to").is_not(None)),
                sa.or_(col("reports_to").is_(None), col("reports_to") != col("id")),
                sa.or_(col("ends_on").is_(None), col("ends_on") >= col("starts_on")),
            ),
            name="org_seats_check",
        ),
    )
    op.create_index("org_seats_unit_idx", "org_seats", ["unit_id"])
    op.create_index("org_seats_user_idx", "org_seats", ["user_id"])
    live = col("ended_at").is_(None)
    op.create_index(
        "org_seats_one_primary_key",
        "org_seats",
        ["user_id"],
        unique=True,
        postgresql_where=sa.and_(col("is_primary"), live, col("user_id").is_not(None)),
    )
    op.create_index(
        "org_seats_one_agent_key",
        "org_seats",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.and_(live, col("profile_id").is_not(None)),
    )


def _ceilings() -> None:
    op.create_table(
        "autonomy_ceilings",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("author_id", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _key("autonomy_ceilings", "author_id", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("scope", "scope_id", "revision", name="autonomy_ceilings_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("scope").in_(SCOPES),
                col("level").in_(LEVELS),
                col("revision") >= 1,
                sa.or_(col("note").is_(None), sa.func.char_length(col("note")) <= MAX_NOTE_CHARS),
            ),
            name="autonomy_ceilings_check",
        ),
    )


def _seed() -> None:
    """The root with its ceiling of A5, and a primary member seat at the root for every user."""
    units = sa.table("org_units", col("id"), col("parent_id"), col("slug"), col("name"), col("kind"))
    ceilings = sa.table("autonomy_ceilings", col("scope"), col("scope_id"), col("revision"), col("level"), col("note"))
    seats = sa.table("org_seats", col("unit_id"), col("user_id"), col("role"), col("is_primary"))
    users = sa.table("users", col("id"))
    op.execute(units.insert().values(slug=ROOT_SLUG, name=ROOT_NAME, kind="org"))
    root = sa.select(units.c.id).where(units.c.parent_id.is_(None)).scalar_subquery()
    first = sa.select(sa.literal("unit"), root, sa.literal(1), sa.literal(ROOT_LEVEL), sa.literal("schema 0099"))
    op.execute(ceilings.insert().from_select(["scope", "scope_id", "revision", "level", "note"], first))
    placed = sa.select(root, users.c.id, sa.literal("member"), sa.true()).order_by(users.c.id)
    op.execute(seats.insert().from_select(["unit_id", "user_id", "role", "is_primary"], placed))


def upgrade() -> None:
    _units()
    _seats()
    _ceilings()
    _seed()


def downgrade() -> None:
    op.drop_table("autonomy_ceilings")
    op.drop_table("org_seats")
    op.drop_table("org_units")
