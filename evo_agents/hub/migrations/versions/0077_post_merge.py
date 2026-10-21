"""What follows a merge of the hub's merge queue: its checks, PROD, the revert of a culprit, and the audit samples.

Revision ID: 0077
Revises: 0076
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_merge``, ``evo_agents.hub.merge_gate`` and
``evo_agents.hub.post_merge`` as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions.

After the queue merged a merge request, the hub follows its commit on the target branch: its UAT check, then the
deploy to PROD and its PROD check, and the rollback of PROD and the revert of the merge request when a check fails
again after a retry (plan gitlab-merge-queue, criterion a6). merge_queue gains what it needs: the merge request a
revert of the hub undoes (``reverts_id``, the source ``revert`` and no other has one), the result of its own UAT and
PROD checks (RESULTS) with when each passed, since when the hub waits for the job it watches, and what it follows on
GitLab (``follow``: the pipeline and the jobs it played, retried or watches, an object). merge_samples holds the merge
requests drawn for an audit sample, one each, and an admin's answer (SAMPLE_RESULTS) with a note. No column of a
table older than 0075 changes.

Going back drops merge_samples, deletes the rows of the reverts the hub opened, drops the columns and puts the check
of 0075 back: the hub of 0076 follows no merge after it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None

SOURCES_0075 = ("plan", "task", "builder", "label")
REVERT = "revert"
SOURCES = (*SOURCES_0075, REVERT)  # evo_agents.hub.merge_gate.SOURCES
STATES = (  # evo_agents.hub.merge_gate.STATES
    "gating",
    "judging",
    "ready",
    "rebasing",
    "testing",
    "merging",
    "merged",
    "verifying",
    "releasing",
    "done",
    "rolling_back",
    "reverting",
    "reverted",
    "blocked",
    "paused",
    "closed",
)
RESULTS = ("passed", "failed", "none")  # evo_agents.hub.post_merge.RESULTS
SAMPLE_RESULTS = ("ok", "faulty")  # evo_agents.hub.post_merge.SAMPLE_RESULTS
MAX_REASON_CHARS = 2000  # evo_agents.hub.merge_gate.MAX_REASON_CHARS
MAX_DESCRIPTION_BYTES = 64 * 1024  # evo_agents.hub.merge_gate.MAX_KEPT_DESCRIPTION_BYTES
MAX_NOTE_CHARS = 2000  # evo_agents.hub.server.post_merge.MAX_NOTE_CHARS
CHECK = "merge_queue_check"

col = sa.column


def _queue_check(sources) -> None:
    """The check of merge_queue as 0075 wrote it, its source one of ``sources``."""
    op.drop_constraint(CHECK, "merge_queue", type_="check")
    op.create_check_constraint(
        CHECK,
        "merge_queue",
        sa.and_(
            col("source").in_(sources),
            col("state").in_(STATES),
            col("iid") >= 1,
            col("judge_attempts") >= 0,
            sa.func.char_length(col("reason")) <= MAX_REASON_CHARS,
            sa.func.octet_length(col("description")) <= MAX_DESCRIPTION_BYTES,
            sa.func.jsonb_typeof(col("steps", postgresql.JSONB)) == "array",
            sa.or_(col("source") != "plan", col("plan_id").is_not(None)),
        ),
    )


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    _queue_check(SOURCES)
    op.add_column("merge_queue", sa.Column("reverts_id", sa.BigInteger(), nullable=True))
    op.add_column("merge_queue", sa.Column("uat_result", sa.Text(), nullable=True))
    op.add_column("merge_queue", _when("uat_at"))
    op.add_column("merge_queue", sa.Column("prod_result", sa.Text(), nullable=True))
    op.add_column("merge_queue", _when("prod_at"))
    op.add_column("merge_queue", _when("waits_since"))
    op.add_column(
        "merge_queue",
        sa.Column("follow", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.create_foreign_key(
        "merge_queue_reverts_id_fkey", "merge_queue", "merge_queue", ["reverts_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("merge_queue_reverts_idx", "merge_queue", ["reverts_id"])
    op.create_check_constraint(
        "merge_queue_after_check",
        "merge_queue",
        sa.and_(
            col("uat_result").in_(RESULTS),
            col("prod_result").in_(RESULTS),
            sa.func.jsonb_typeof(col("follow", postgresql.JSONB)) == "object",
            (col("source") == REVERT) == col("reverts_id").is_not(None),
        ),
    )
    op.create_table(
        "merge_samples",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("queue_id", sa.BigInteger(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("answered_by", sa.BigInteger(), nullable=True),
        _when("answered_at"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="merge_samples_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["queue_id"], ["merge_queue.id"], ondelete="CASCADE", name="merge_samples_queue_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["answered_by"], ["users.id"], ondelete="SET NULL", name="merge_samples_answered_by_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="merge_samples_pkey"),
        sa.UniqueConstraint("queue_id", name="merge_samples_queue_id_key"),
        sa.CheckConstraint(
            sa.and_(
                col("result").in_(SAMPLE_RESULTS),
                col("result").is_(None) == col("answered_at").is_(None),
                sa.func.char_length(col("note")).between(1, MAX_NOTE_CHARS),
            ),
            name="merge_samples_check",
        ),
    )
    op.create_index("merge_samples_project_idx", "merge_samples", ["project_id", "id"])


def downgrade() -> None:
    op.drop_table("merge_samples")
    queue = sa.table("merge_queue", col("source"))
    op.execute(queue.delete().where(queue.c.source == REVERT))
    op.drop_constraint("merge_queue_after_check", "merge_queue", type_="check")
    op.drop_index("merge_queue_reverts_idx", table_name="merge_queue")
    op.drop_constraint("merge_queue_reverts_id_fkey", "merge_queue", type_="foreignkey")
    for name in ("follow", "waits_since", "prod_at", "prod_result", "uat_at", "uat_result", "reverts_id"):
        op.drop_column("merge_queue", name)
    _queue_check(SOURCES_0075)
