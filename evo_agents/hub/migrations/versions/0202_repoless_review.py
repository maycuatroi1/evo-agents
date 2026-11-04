"""A review run with no repo: the KG init of a project fed by uploads alone.

Revision ID: 0202
Revises: 0201
Create Date: 2026-11-04

A project made by an upload (plan kg-upload-archive) has no repo, and its KG init is a review run that reads the
hub's graph alone (``evo_agents.hub.server.kg_init``): runs.repos of a review may now be null, as a history run's
is. runs_kind_repos_check takes ``review`` among the kinds that may have no repos; the kinds below are copies of
0131's, never imports, so a later change takes a new revision. No other table changes.

Going back deletes the review runs with no repos and puts the check of 0131 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0202"
down_revision = "0201"
branch_labels = None
depends_on = None

ANY_REPOS_0131 = ("chat", "local", "task", "history", "question", "browser")  # as 0131 set it
ANY_REPOS = (*ANY_REPOS_0131, "review")
col = sa.column


def _check(any_repos) -> None:
    kind = col("kind")
    op.drop_constraint("runs_kind_repos_check", "runs", type_="check")
    op.create_check_constraint(
        "runs_kind_repos_check", "runs", sa.or_(kind.in_(any_repos), (kind == "step") == col("repos").is_(None))
    )


def upgrade() -> None:
    _check(ANY_REPOS)


def downgrade() -> None:
    runs = sa.table("runs", col("kind"), col("repos"))
    op.execute(sa.delete(runs).where(runs.c.kind == "review", runs.c.repos.is_(None)))
    _check(ANY_REPOS_0131)
