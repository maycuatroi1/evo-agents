"""The decisions writers take on a project's knowledge graph, and the proposals agents make.

Revision ID: 0142
Revises: 0141
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.tables_kg`` and ``evo_agents.hub.kg_decisions`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

kg_decisions holds one row per decision of a project's graph (plan kg-map-and-review, criterion a6), numbered from 1
in the project: its kind (KINDS), its state (STATES; an orphan is an active decision the latest build no longer
holds the subject of, which the hub works out when it reads, and never stores) and its subject. A deprecation names a
node (src alone) or an edge (src, rel and dst); a merge names two nodes (src and dst); a link names an edge to add
(src, rel and dst).

kg_decision_events holds each step of a decision's history (ACTIONS): who, when, why (1 to MAX_REASON_CHARS once
trimmed) and the build the graph was at, keyed by the project's revision the step made, so the revision a writer read
tells whether anything changed since. A project's decisions and events go with it, a decision's events with it, a
member an event names is not deleted under it, and a build that goes leaves the event without one. No other table
changes.

Going back drops both tables.
"""

import sqlalchemy as sa
from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None

DECISIONS = "kg_decisions"
EVENTS = "kg_decision_events"
KINDS = ("deprecate", "merge", "link")
STATES = ("proposed", "active", "rejected", "undone")
ACTIONS = ("propose", "decide", "accept", "reject", "undo")
MAX_REASON_CHARS = 1000
MAX_REF_CHARS = 1000

col = sa.column


def _key(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _ref(name: str):
    return sa.func.char_length(col(name)).between(1, MAX_REF_CHARS)


def _shape():
    """What each kind names: a deprecation a node or an edge, a merge two nodes, a link an edge."""
    rel, dst = col("rel"), col("dst")
    edge = sa.and_(rel.is_not(None), dst.is_not(None), _ref("rel"), _ref("dst"))
    return sa.or_(
        sa.and_(col("kind") == "deprecate", sa.or_(sa.and_(rel.is_(None), dst.is_(None)), edge)),
        sa.and_(col("kind") == "merge", rel.is_(None), dst.is_not(None), _ref("dst"), dst != col("src")),
        sa.and_(col("kind") == "link", edge),
    )


def upgrade() -> None:
    op.create_table(
        DECISIONS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("src", sa.Text(), nullable=False),
        sa.Column("rel", sa.Text(), nullable=True),
        sa.Column("dst", sa.Text(), nullable=True),
        _key(DECISIONS, "project_id", "projects", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=f"{DECISIONS}_pkey"),
        sa.UniqueConstraint("project_id", "number", name=f"{DECISIONS}_project_id_number_key"),
        sa.CheckConstraint(
            sa.and_(col("number") >= 1, col("kind").in_(KINDS), col("state").in_(STATES), _ref("src"), _shape()),
            name=f"{DECISIONS}_check",
        ),
    )
    op.create_table(
        EVENTS,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False),
        sa.Column("decision_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("build_id", sa.BigInteger(), nullable=True),
        _key(EVENTS, "project_id", "projects", ondelete="CASCADE"),
        _key(EVENTS, "decision_id", DECISIONS, ondelete="CASCADE"),
        _key(EVENTS, "actor_id", "users", ondelete="RESTRICT"),
        _key(EVENTS, "build_id", "kg_builds", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("project_id", "rev", name=f"{EVENTS}_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("rev") >= 1,
                col("action").in_(ACTIONS),
                sa.func.char_length(sa.func.btrim(col("reason"))).between(1, MAX_REASON_CHARS),
            ),
            name=f"{EVENTS}_check",
        ),
    )
    op.create_index(f"{EVENTS}_decision_idx", EVENTS, ["decision_id"])


def downgrade() -> None:
    op.drop_table(EVENTS)
    op.drop_table(DECISIONS)
