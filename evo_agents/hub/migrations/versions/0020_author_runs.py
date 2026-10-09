"""The author run: a run that writes an execution plan from a member's request.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-09

``evo_agents.hub.author`` describes the design; the lists and bounds below are copies of its constants as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the table.

runs.kind takes a fifth value, 'author': a run with repos as a plan run has (the project's harness first), no step key
or repo, and the member's request in runs.request, 1 to 16 KiB of UTF-8, exactly for that kind. An author run of a new
plan works on no plan, so its plan_id is NULL as a review run's is; the check that tied a NULL plan_id to the kind
'review' alone now lets an author run have one or not.

Going back deletes the author runs, drops the column and puts the checks of 0019 back, which is the schema that schema
0019 runs on.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

KINDS_0019 = ("step", "plan", "review", "judge")
KINDS = ("step", "plan", "review", "judge", "author")  # evo_agents.hub.runs.RUN_KINDS
MAX_REQUEST_BYTES = 16 * 1024  # evo_agents.hub.author.MAX_REQUEST_BYTES

col = sa.column


def upgrade() -> None:
    op.drop_constraint("runs_kind_check", "runs", type_="check")
    op.create_check_constraint("runs_kind_check", "runs", col("kind").in_(KINDS))
    op.drop_constraint("runs_kind_plan_check", "runs", type_="check")
    op.create_check_constraint(
        "runs_kind_plan_check",
        "runs",
        sa.or_(col("kind") == "author", (col("kind") == "review") == col("plan_id").is_(None)),
    )
    op.add_column("runs", sa.Column("request", sa.Text(), nullable=True))
    op.create_check_constraint(
        "runs_author_request_check", "runs", (col("kind") == "author") == col("request").is_not(None)
    )
    op.create_check_constraint(
        "runs_request_size_check", "runs", sa.func.octet_length(col("request")).between(1, MAX_REQUEST_BYTES)
    )


def downgrade() -> None:
    runs = sa.table("runs", col("kind"), col("attempt"), col("resume_of_run_id"))
    op.drop_constraint("runs_request_size_check", "runs", type_="check")
    op.drop_constraint("runs_author_request_check", "runs", type_="check")
    # the runs that go on from a parked author run, then the retries, which name the attempt before them
    op.execute(runs.delete().where(runs.c.kind == "author", runs.c.resume_of_run_id.is_not(None)))
    for attempt in (3, 2, 1):
        op.execute(runs.delete().where(runs.c.kind == "author", runs.c.attempt == attempt))
    op.drop_column("runs", "request")
    op.drop_constraint("runs_kind_plan_check", "runs", type_="check")
    op.create_check_constraint("runs_kind_plan_check", "runs", (col("kind") == "review") == col("plan_id").is_(None))
    op.drop_constraint("runs_kind_check", "runs", type_="check")
    op.create_check_constraint("runs_kind_check", "runs", col("kind").in_(KINDS_0019))
