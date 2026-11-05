"""When the hub told the sender of an upload and the project's admins how the flow after it ended.

Revision ID: 0206
Revises: 0205
Create Date: 2026-11-05

kg_uploads.told_at is new (plan kg-zip-to-answers, step 4, ``evo_agents.hub.server.kg_pipeline``): the moment the hub
sent the notice ``kg_pipeline`` because the flow after the upload (read, built, the starter questions, the Curator's
kg_init run, the entities step) was ready or failed at a phase, or found it replaced by a later upload. It is NULL
while the flow moves. The uploads made before this revision are marked told now, so nobody hears of old uploads. The
notice kind needs no change of the check on notifications.notice_kind: its name has the shape of a plugin's kind.

Going back drops the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0206"
down_revision = "0205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kg_uploads", sa.Column("told_at", sa.DateTime(timezone=True), nullable=True))
    uploads = sa.table("kg_uploads", sa.column("told_at", sa.DateTime(timezone=True)))
    op.execute(sa.update(uploads).values(told_at=sa.func.now()))


def downgrade() -> None:
    op.drop_column("kg_uploads", "told_at")
