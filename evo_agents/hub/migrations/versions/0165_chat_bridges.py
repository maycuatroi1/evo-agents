"""Bridges of group chats to groups of a core plugin's channel: chat_bridges, their one-time codes and the ids of the
messages they carry, the column chat_messages.via, and the event chat.bridge.

Revision ID: 0165
Revises: 0164
Create Date: 2026-10-27

chat_bridges holds each link of a hub group chat to a group of a kind of channel (``external_id``, the group's id
there), of the core plugin that contributes the kind (``plugin``, its id), with the config and the state that plugin
keeps, who linked it and when, and who unlinked it, when and from where (``unlinked_via``: web, cli, bot or admin).
A group bridges one chat at a time: chat_bridges_external_key is unique on (kind, external_id) while it is linked.
chat_bridge_codes holds the one-time codes a chat admin makes from a web session, which the plugin redeems from the
group: their SHA-256 alone, ten minutes, bound to that session. chat_bridge_messages maps a hub message to the group's
message it came from (``in``) or went out as (``out``), so replies thread both ways; a group's message id maps once per
bridge. chat_messages.via names the kind of channel a message came in by. plugin_deliveries takes the kind
``chat.bridge``, the event a bridge's plugin gets of a new message, a run's progress or a reaction
(``evo_agents.hub.chat_bridge_events``). ``evo_agents.hub.tables_chats`` describes the tables and the column; the
CHECKs of plugin_deliveries are this file's alone. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

Going back deletes the deliveries of chat.bridge and drops the three tables and the column: the messages that came in
from a group stay, as messages of their senders.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0165"
down_revision = "0164"
branch_labels = None
depends_on = None

DELIVERY_KINDS_0164 = (  # plugin_deliveries.kind as 0100 set it
    "run.state",
    "chat.message",
    "plan.step",
    "notification",
    "task.result",
    "canvas.claim",
    "canvas.baseline",
    "signal.posted",
    "srs.version.approved",
    "tasks.status",
    "agent.answer",
)
BRIDGE_EVENT = "chat.bridge"  # evo_agents.hub.chat_bridge_events.EVENT
DELIVERY_KINDS = (*DELIVERY_KINDS_0164, BRIDGE_EVENT)  # evo_agents.hub.plugin_events.KINDS
DELIVERY_STATES = ("pending", "delivered", "failed")
NAME_PATTERN = "^[a-z][a-z0-9_-]{0,62}$"  # a kind of channel, as a manifest names it
MAX_ERROR_CHARS = 2000
MAX_EXTERNAL_CHARS = 200  # chat_bridges.MAX_EXTERNAL_CHARS
UNLINKED_VIA = ("web", "cli", "bot", "admin")
DIRECTIONS = ("in", "out")

col = sa.column


def _deliveries(kinds) -> None:
    kind = col("kind")
    op.drop_constraint("plugin_deliveries_check", "plugin_deliveries", type_="check")
    op.create_check_constraint(
        "plugin_deliveries_check",
        "plugin_deliveries",
        sa.and_(
            kind.in_(kinds),
            col("contribution").regexp_match(NAME_PATTERN),
            col("state").in_(DELIVERY_STATES),
            col("attempts") >= 0,
            sa.func.char_length(col("last_error")) <= MAX_ERROR_CHARS,
            (col("state") == "delivered") == col("delivered_at").is_not(None),
        ),
    )


def _fk(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _external(name: str = "external_id"):
    return sa.func.char_length(col(name)).between(1, MAX_EXTERNAL_CHARS)


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _object(name: str):
    return sa.func.jsonb_typeof(col(name, JSONB())) == "object"


def upgrade() -> None:
    op.create_table(
        "chat_bridges",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("config", JSONB(), nullable=False, server_default="{}"),
        sa.Column("state", JSONB(), nullable=False, server_default="{}"),
        sa.Column("linked_by", sa.BigInteger()),
        _stamp("linked_at"),
        sa.Column("unlinked_at", sa.DateTime(timezone=True)),
        sa.Column("unlinked_by", sa.BigInteger()),
        sa.Column("unlinked_via", sa.Text()),
        _fk("chat_bridges", "chat_id", "chats.id", "CASCADE"),
        _fk("chat_bridges", "linked_by", "users.id", ondelete="SET NULL"),
        _fk("chat_bridges", "unlinked_by", "users.id", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="chat_bridges_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").regexp_match(NAME_PATTERN),
                _external(),
                _object("config"),
                _object("state"),
                sa.or_(col("unlinked_via").is_(None), col("unlinked_via").in_(UNLINKED_VIA)),
                col("unlinked_at").is_(None) == col("unlinked_via").is_(None),
            ),
            name="chat_bridges_check",
        ),
    )
    linked = col("unlinked_at").is_(None)
    op.create_index(
        "chat_bridges_external_key", "chat_bridges", ["kind", "external_id"], unique=True, postgresql_where=linked
    )
    op.create_index("chat_bridges_chat_idx", "chat_bridges", ["chat_id"], postgresql_where=linked)
    op.create_table(
        "chat_bridge_codes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        _stamp("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        _fk("chat_bridge_codes", "chat_id", "chats.id", "CASCADE"),
        _fk("chat_bridge_codes", "user_id", "users.id", "CASCADE"),
        _fk("chat_bridge_codes", "token_id", "tokens.id", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name="chat_bridge_codes_pkey"),
        sa.UniqueConstraint("code_hash", name="chat_bridge_codes_code_hash_key"),
        sa.CheckConstraint(col("kind").regexp_match(NAME_PATTERN), name="chat_bridge_codes_kind_check"),
    )
    op.create_index("chat_bridge_codes_user_idx", "chat_bridge_codes", ["user_id"])
    op.create_table(
        "chat_bridge_messages",
        sa.Column("bridge_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        _stamp("created_at"),
        _fk("chat_bridge_messages", "bridge_id", "chat_bridges.id", "CASCADE"),
        _fk("chat_bridge_messages", "message_id", "chat_messages.id", "CASCADE"),
        sa.PrimaryKeyConstraint("bridge_id", "message_id", "direction", name="chat_bridge_messages_pkey"),
        sa.UniqueConstraint("bridge_id", "external_id", name="chat_bridge_messages_external_key"),
        sa.CheckConstraint(sa.and_(col("direction").in_(DIRECTIONS), _external()), name="chat_bridge_messages_check"),
    )
    op.create_index("chat_bridge_messages_message_idx", "chat_bridge_messages", ["message_id"])
    op.add_column("chat_messages", sa.Column("via", sa.Text(), nullable=True))
    op.create_check_constraint(
        "chat_messages_via_check", "chat_messages", sa.or_(col("via").is_(None), col("via").regexp_match(NAME_PATTERN))
    )
    _deliveries(DELIVERY_KINDS)


def downgrade() -> None:
    deliveries = sa.table("plugin_deliveries", col("kind"))
    op.execute(sa.delete(deliveries).where(deliveries.c.kind == BRIDGE_EVENT))
    _deliveries(DELIVERY_KINDS_0164)
    op.drop_constraint("chat_messages_via_check", "chat_messages", type_="check")
    op.drop_column("chat_messages", "via")
    op.drop_table("chat_bridge_messages")
    op.drop_table("chat_bridge_codes")
    op.drop_table("chat_bridges")
