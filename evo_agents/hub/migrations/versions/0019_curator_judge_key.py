"""The key of a judge run: what it reads and its verdict take more than the worker's token.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-09

``evo_agents.hub.server.changes`` describes the design. curator_changes.judge_key holds the SHA-256, in hex, of the key
the hub made when the change's judge run was claimed, which the claim handed the daemon alone: GET
/v1/worker/runs/{id}/judge and POST /v1/worker/runs/{id}/verdict take that key besides the worker token, so code on the
worker's machine that reads the worker token neither reads the project's hidden checks nor writes a verdict. The key
goes once the verdict is in, and a later claim makes another. The statement is an Alembic operation on SQLAlchemy Core
expressions, as ``evo_agents.hub.tables`` describes the table.

Going back drops the column: a judge run in flight then takes the worker token alone again, as schema 0018 did.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("curator_changes", sa.Column("judge_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("curator_changes", "judge_key")
