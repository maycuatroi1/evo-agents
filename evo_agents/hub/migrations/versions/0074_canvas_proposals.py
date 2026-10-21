"""The proposals of the core plugin canvas: a new claim, or a change of a claim at the rev it was read at, that an
agent or the hub proposes for a writer to accept or reject.

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_canvas`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

canvas_proposals holds what a task run of @strategist (and later the value check and the Loop) proposes instead of
writing it: a new claim (``create``, no claim and no base rev until a writer accepts it) or a change of a claim at
``base_rev`` (``change``), the fields it sets, why, whether it rests on simulated users, its provenance as a rev has
it, and its status: open; accepted, with the claim and the rev the acceptance wrote and the writer who accepted it;
rejected, by whom and why; or stale, once the claim changed since its base rev.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

ACTORS = ("person", "agent", "hub")
PROPOSAL_KINDS = ("create", "change")
PROPOSAL_STATUSES = ("open", "accepted", "rejected", "stale")
MAX_REASON_CHARS = 1_000

col = sa.column
NAME = "canvas_proposals"
INDEXES = ("canvas_proposals_claim_idx", "canvas_proposals_canvas_idx")


def _key(column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{NAME}_{column}_fkey")


def _length(name: str, low: int, high: int):
    return sa.func.char_length(col(name)).between(low, high)


def _check():
    decided = col("status").in_(("accepted", "rejected"))
    return sa.and_(
        col("kind").in_(PROPOSAL_KINDS),
        col("status").in_(PROPOSAL_STATUSES),
        sa.func.jsonb_typeof(col("fields")) == "object",
        _length("reason", 1, MAX_REASON_CHARS),
        col("actor").in_(ACTORS),
        sa.or_(col("actor") == "hub", col("user_id").is_not(None)),
        # a change names its claim and the rev it was read at; a new claim has neither until it is accepted
        sa.or_(
            sa.and_(col("kind") == "change", col("claim_id").is_not(None), col("base_rev") >= 1),
            sa.and_(col("kind") == "create", col("base_rev").is_(None)),
        ),
        sa.or_(col("kind") == "change", col("status") == "accepted", col("claim_id").is_(None)),
        # accepted names the claim and the rev it wrote; accepted or rejected names who decided and when
        (col("status") == "accepted") == sa.and_(col("rev").is_not(None), col("claim_id").is_not(None)),
        decided == col("decided_by").is_not(None),
        decided == col("decided_at").is_not(None),
        sa.or_(
            col("decided_reason").is_(None),
            sa.and_(col("status") == "rejected", _length("decided_reason", 1, MAX_REASON_CHARS)),
        ),
    )


def upgrade() -> None:
    op.create_table(
        NAME,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("canvas_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=True),
        sa.Column("base_rev", sa.Integer(), nullable=True),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("runtime", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("chat_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("rev", sa.Integer(), nullable=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_reason", sa.Text(), nullable=True),
        _key("canvas_id", "canvas_canvases", ondelete="CASCADE"),
        _key("claim_id", "canvas_claims", ondelete="CASCADE"),
        _key("user_id", "users", ondelete="RESTRICT"),
        _key("run_id", "runs", ondelete="SET NULL"),
        _key("chat_message_id", "chat_messages", ondelete="SET NULL"),
        _key("decided_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="canvas_proposals_pkey"),
        sa.CheckConstraint(_check(), name="canvas_proposals_check"),
    )
    op.create_index("canvas_proposals_canvas_idx", NAME, ["canvas_id", "status", "id"])
    op.create_index("canvas_proposals_claim_idx", NAME, ["claim_id", "status"])


def downgrade() -> None:
    for index in INDEXES:
        op.drop_index(index, table_name=NAME)
    op.drop_table(NAME)
