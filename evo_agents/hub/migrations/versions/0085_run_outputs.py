"""The outputs of runs, their links to questions and chat messages, and a project's switch of tool images.

Revision ID: 0085
Revises: 0084
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.outputs`` and ``evo_agents.hub.tables_outputs`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

run_outputs holds each file of a run that the hub keeps (plan run-outputs): its run and project, its source (SOURCES),
name, media type, size, SHA-256, caption and the tool call it came from; one row per SHA-256 a run. Its bytes are a
blob of kind run-output that the project holds. run_output_links ties an output to a decision or to a chat message,
exactly one of the two a row. projects.output_tool_images, true by default, is a project's switch of the images taken
out of tools' results. chat_events.kind takes ``message_changed``: outputs of a run went under a message already
written. No column of an older table changes.

Going back deletes the message_changed events, drops the two tables and the switch; the blobs rows of kind run-output
stay, as any blob a project holds, for the collection of unreferenced blobs to treat as it treats the others.
"""

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

SOURCES = ("sent", "tool", "outputs")  # evo_agents.hub.outputs.SOURCES
MAX_NAME_CHARS = 255  # evo_agents.hub.outputs.MAX_NAME_CHARS
MAX_MEDIA_TYPE_CHARS = 255  # two parts of at most 127 and a slash (evo_agents.hub.outputs.MEDIA_TYPE)
MAX_CAPTION_CHARS = 2000  # evo_agents.hub.outputs.MAX_CAPTION_CHARS
MAX_TOOL_CALL_CHARS = 200  # evo_agents.hub.outputs.MAX_TOOL_CALL_CHARS
MAX_FILE_BYTES = 200 * 1024 * 1024  # evo_agents.hub.outputs.MAX_FILE_BYTES
SHA256 = "^[0-9a-f]{64}$"
CHAT_EVENT_KINDS_0051 = (  # chat_events_kind_check as 0051 set it
    "message",
    "reaction_added",
    "reaction_removed",
    "member_added",
    "member_removed",
    "image_changed",
)
MESSAGE_CHANGED = "message_changed"  # evo_agents.hub.chats.MESSAGE_CHANGED
CHAT_EVENT_KINDS = (*CHAT_EVENT_KINDS_0051, MESSAGE_CHANGED)  # evo_agents.hub.chats.EVENT_KINDS

col = sa.column


def _chat_event_kinds(kinds) -> None:
    op.drop_constraint("chat_events_kind_check", "chat_events", type_="check")
    op.create_check_constraint("chat_events_kind_check", "chat_events", col("kind").in_(kinds))


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target], ondelete="CASCADE", name=f"{table}_{column}_fkey")


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.add_column("projects", sa.Column("output_tool_images", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.create_table(
        "run_outputs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("tool_call_id", sa.Text(), nullable=True),
        _stamp("created_at"),
        _fk("run_outputs", "run_id", "runs.id"),
        _fk("run_outputs", "project_id", "projects.id"),
        sa.PrimaryKeyConstraint("id", name="run_outputs_pkey"),
        sa.UniqueConstraint("run_id", "sha256", name="run_outputs_run_id_sha256_key"),
        sa.CheckConstraint(
            sa.and_(
                col("source").in_(SOURCES),
                sa.func.char_length(col("name")).between(1, MAX_NAME_CHARS),
                sa.func.char_length(col("media_type")).between(3, MAX_MEDIA_TYPE_CHARS),
                col("size").between(0, MAX_FILE_BYTES),
                col("sha256").regexp_match(SHA256),
                sa.func.char_length(col("caption")).between(1, MAX_CAPTION_CHARS),
                sa.func.char_length(col("tool_call_id")).between(1, MAX_TOOL_CALL_CHARS),
            ),
            name="run_outputs_check",
        ),
    )
    op.create_index("run_outputs_sha256_idx", "run_outputs", ["sha256"])
    op.create_table(
        "run_output_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("output_id", sa.BigInteger(), nullable=False),
        sa.Column("decision_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_message_id", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _fk("run_output_links", "output_id", "run_outputs.id"),
        _fk("run_output_links", "decision_id", "decisions.id"),
        _fk("run_output_links", "chat_message_id", "chat_messages.id"),
        sa.PrimaryKeyConstraint("id", name="run_output_links_pkey"),
        sa.UniqueConstraint("output_id", "decision_id", name="run_output_links_decision_key"),
        sa.UniqueConstraint("output_id", "chat_message_id", name="run_output_links_message_key"),
        sa.CheckConstraint(
            sa.func.num_nonnulls(col("decision_id"), col("chat_message_id")) == 1, name="run_output_links_check"
        ),
    )
    op.create_index(
        "run_output_links_decision_idx",
        "run_output_links",
        ["decision_id"],
        postgresql_where=col("decision_id").is_not(None),
    )
    op.create_index(
        "run_output_links_message_idx",
        "run_output_links",
        ["chat_message_id"],
        postgresql_where=col("chat_message_id").is_not(None),
    )
    _chat_event_kinds(CHAT_EVENT_KINDS)


def downgrade() -> None:
    events = sa.table("chat_events", col("kind"))
    op.execute(sa.delete(events).where(events.c.kind == MESSAGE_CHANGED))
    _chat_event_kinds(CHAT_EVENT_KINDS_0051)
    op.drop_table("run_output_links")
    op.drop_table("run_outputs")
    op.drop_column("projects", "output_tool_images")
