"""The chat of an author run: it waits for its owner's reply, is parked and resumed, and its owner ends it.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-09

``evo_agents.hub.author`` describes the design; the lists below are copies of its constants and of
``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the table.

An author run that waited too long for its owner's reply is parked as a plan run is, and a reply queues the run that
resumes it: runs_resume_check, which let only a plan run go on from another, now lets an author run go on from
another too. runs.finish_requested_at is when the owner ended the chat of an author run that a worker holds; the next
heartbeat tells the worker, which ends the run done. Only an author run has one.

notifications.notice_kind takes a seventh value, 'author_waiting': an author run waits for its owner's reply.

Going back deletes the notices author_waiting, makes each author run that resumes another one of its own (its
resume_of_run_id NULL, which the check of 0010 takes), drops the column and puts the checks of 0021 back, which is the
schema that schema 0021 runs on.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

NOTICE_KINDS_0021 = (
    "push_default_branch",
    "merge_default_branch",
    "plan_finished",
    "run_failed",
    "curator_brief",
    "curator_paused",
)
NOTICE_KINDS = (*NOTICE_KINDS_0021, "author_waiting")  # evo_agents.hub.runs.NOTICE_KINDS
RESUME_KINDS = ("plan", "author")  # evo_agents.hub.runs.RESUME_KINDS

col = sa.column


def _notice_kinds(kinds) -> None:
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(kinds))


def _resume_check(resumes) -> None:
    op.create_check_constraint(
        "runs_resume_check",
        "runs",
        sa.or_(col("resume_of_run_id").is_(None), sa.and_(resumes, col("resume_of_run_id") != col("id"))),
    )


def upgrade() -> None:
    op.drop_constraint("runs_resume_check", "runs", type_="check")
    _resume_check(col("kind").in_(RESUME_KINDS))
    op.add_column("runs", sa.Column("finish_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "runs_finish_check", "runs", sa.or_(col("finish_requested_at").is_(None), col("kind") == "author")
    )
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    _notice_kinds(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    runs = sa.table("runs", col("kind"), col("resume_of_run_id"))
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.execute(notifications.delete().where(notifications.c.notice_kind == "author_waiting"))
    _notice_kinds(NOTICE_KINDS_0021)
    op.drop_constraint("runs_finish_check", "runs", type_="check")
    op.drop_column("runs", "finish_requested_at")
    op.drop_constraint("runs_resume_check", "runs", type_="check")
    op.execute(runs.update().where(runs.c.kind == "author").values(resume_of_run_id=None))
    _resume_check(col("kind") == "plan")
