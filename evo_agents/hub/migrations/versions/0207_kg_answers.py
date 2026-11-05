"""The answer jobs of ``hub kg answer`` and each question they ask.

Revision ID: 0207
Revises: 0206
Create Date: 2026-11-05

kg_answer_jobs and kg_answers are new (plan kg-zip-to-answers, step 5, ``evo_agents.hub.server.kg_answers``). A job is
one file of questions a writer sent to a project: who sent it, the SHA-256 of its ids and questions in order
(``digest``, which a second send of the same file finds), how many questions, how many go to one agent run, the run
asking the current batch, how many runs it queued, since when it waits for a pool, and its state: running, then done
or failed. kg_answers holds each question of a job at its position in the file, with its id, the run that asked it,
the agent's answer and evidence, or the error it ended with. The tables start empty. No other table changes.

The states below are copies of ``evo_agents.hub.tables_kg.ANSWER_STATES`` as of this revision, never imports, so a later
change to them takes a new revision.

Going back drops both tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0207"
down_revision = "0206"
branch_labels = None
depends_on = None

JOBS = "kg_answer_jobs"
ANSWERS = "kg_answers"
STATES = ("running", "done", "failed")

col = sa.column


def _key(table: str, column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        JOBS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("questions", sa.Integer(), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("runs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _key(JOBS, "project_id", "projects", ondelete="CASCADE"),
        _key(JOBS, "created_by", "users", ondelete="SET NULL"),
        _key(JOBS, "run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{JOBS}_pkey"),
        sa.CheckConstraint(col("state").in_(STATES), name=f"{JOBS}_state_check"),
    )
    op.create_index(f"{JOBS}_project_idx", JOBS, ["project_id", "id"])
    op.create_table(
        ANSWERS,
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("qid", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        _key(ANSWERS, "job_id", JOBS, ondelete="CASCADE"),
        _key(ANSWERS, "run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("job_id", "position", name=f"{ANSWERS}_pkey"),
        sa.UniqueConstraint("job_id", "qid", name=f"{ANSWERS}_job_id_qid_key"),
    )
    op.create_index(f"{ANSWERS}_run_idx", ANSWERS, ["run_id"])


def downgrade() -> None:
    op.drop_table(ANSWERS)
    op.drop_table(JOBS)
