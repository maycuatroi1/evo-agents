"""The level of a chat's label a project admin sets, and sinks of kind channel.

Revision ID: 0164
Revises: 0163
Create Date: 2026-10-27

chats.label_override holds the level an admin of every project with an agent in a chat set for it, and the projects it
lowers: {"level": <a level>, "projects": [<project names>]}, or NULL when the chat's label is its projects' default
(``evo_agents.hub.server.chat_labels``). chats_label_override_check keeps it an object. project_sinks_kind_check
takes ``channel``, a sink naming a kind of channel of a core plugin by its id (knowledge.schema.json, the seam
knowledge-manifest-v1). ``evo_agents.hub.tables_chats`` describes the column; the CHECKs are this file's alone. Every
statement is an Alembic operation on SQLAlchemy Core expressions.

Going back deletes the sinks of kind channel and drops the column: every chat's label is its projects' default again,
and the messages sent meanwhile keep the label they were sent with.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0164"
down_revision = "0163"
branch_labels = None
depends_on = None

CHANNEL = "channel"  # evo_agents.hub.access.CHANNEL_KIND
SINK_KINDS_0001 = ("agent-session", "llm-backend", "writeback-target", "cli", "hub")
SINK_KINDS = (*SINK_KINDS_0001, CHANNEL)  # the kinds of knowledge.schema.json

col = sa.column


def _sink_kinds(kinds) -> None:
    op.drop_constraint("project_sinks_kind_check", "project_sinks", type_="check")
    op.create_check_constraint("project_sinks_kind_check", "project_sinks", col("kind").in_(kinds))


def upgrade() -> None:
    op.add_column("chats", sa.Column("label_override", JSONB(), nullable=True))
    override = sa.or_(col("label_override").is_(None), sa.func.jsonb_typeof(col("label_override", JSONB())) == "object")
    op.create_check_constraint("chats_label_override_check", "chats", override)
    _sink_kinds(SINK_KINDS)


def downgrade() -> None:
    sinks = sa.table("project_sinks", col("kind"))
    op.execute(sa.delete(sinks).where(sinks.c.kind == CHANNEL))
    _sink_kinds(SINK_KINDS_0001)
    op.drop_constraint("chats_label_override_check", "chats", type_="check")
    op.drop_column("chats", "label_override")
