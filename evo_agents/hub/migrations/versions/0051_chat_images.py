"""Chat images: the image a group chat's members set, kept in the blob store.

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-20

chats.image_sha256 and chats.image_size name the blob of a group chat's image, both set or neither (chats_image_check);
a direct chat has none, its face being its agent's avatar. The chat's row is what holds the blob: ``evo_agents.hub.
blob_gc`` counts the column among the references to a blob, through the partial index chats_image_sha256_idx, so the
collection of unreferenced blobs leaves an image a chat shows and takes one no chat shows any longer.
chat_events_kind_check takes ``image_changed``, the event of a member setting the image or taking it off.
``evo_agents.hub.tables_chats`` describes the columns and the index; the CHECKs are this file's alone. Every
statement is an Alembic operation on SQLAlchemy Core expressions.

Going back deletes the image_changed events and drops the columns: the chats lose their images, whose objects stay in
the bucket with nothing referring to them, as the branding's images do going back past 0037.
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

GROUP = "group"  # evo_agents.hub.chats.GROUP
IMAGE_CHANGED = "image_changed"  # evo_agents.hub.chats.IMAGE_CHANGED
EVENT_KINDS_0031 = ("message", "reaction_added", "reaction_removed", "member_added", "member_removed")
EVENT_KINDS = (*EVENT_KINDS_0031, IMAGE_CHANGED)  # evo_agents.hub.chats.EVENT_KINDS
HEX_SHA256 = "^[0-9a-f]{64}$"

col = sa.column


def _event_kinds(kinds) -> None:
    op.drop_constraint("chat_events_kind_check", "chat_events", type_="check")
    op.create_check_constraint("chat_events_kind_check", "chat_events", col("kind").in_(kinds))


def upgrade() -> None:
    op.add_column("chats", sa.Column("image_sha256", sa.Text(), nullable=True))
    op.add_column("chats", sa.Column("image_size", sa.BigInteger(), nullable=True))
    image = sa.or_(
        sa.and_(col("image_sha256").is_(None), col("image_size").is_(None)),
        sa.and_(
            col("kind") == GROUP,
            col("image_sha256").is_not(None),
            col("image_size").is_not(None),
            col("image_sha256").regexp_match(HEX_SHA256),
            col("image_size") > 0,
        ),
    )
    op.create_check_constraint("chats_image_check", "chats", image)
    op.create_index(
        "chats_image_sha256_idx", "chats", ["image_sha256"], postgresql_where=col("image_sha256").is_not(None)
    )
    _event_kinds(EVENT_KINDS)


def downgrade() -> None:
    events = sa.table("chat_events", col("kind"))
    op.execute(sa.delete(events).where(events.c.kind == IMAGE_CHANGED))
    _event_kinds(EVENT_KINDS_0031)
    op.drop_index("chats_image_sha256_idx", table_name="chats")
    op.drop_constraint("chats_image_check", "chats", type_="check")
    op.drop_column("chats", "image_size")
    op.drop_column("chats", "image_sha256")
