"""A run's inbox holds a whole answer: run_inbox.body up to 32 KiB.

Revision ID: 0261
Revises: 0260
Create Date: 2026-11-20

Problem P-24 of evo-agents: an agent that asked another agent got its answer cut. The hub clipped the message that wakes
the asking run to 4 KiB and a message of the thread to an agent's inbox to 8 KiB, the bound 0009 gave run_inbox.body,
while an agent's answer and a chat message reach 16 KiB (``evo_agents.hub.chats.MAX_MESSAGE_BYTES``). The check
run_inbox_body_check now bounds the body by ``evo_agents.hub.runs.MAX_INBOX_BYTES``, room for a whole chat message
with the lines the hub puts around it. An empty body stays allowed, as 0109 made it for a message of files alone.

Going back cuts each longer body to its first 2,000 characters, at most 8,000 bytes of UTF-8, before it puts back the
8 KiB bound.
"""

import sqlalchemy as sa
from alembic import op

revision = "0261"
down_revision = "0260"
branch_labels = None
depends_on = None

CHECK = "run_inbox_body_check"
MAX_INBOX_BYTES = 32 * 1024  # evo_agents.hub.runs.MAX_INBOX_BYTES
OLD_MAX_BYTES = 8 * 1024  # the bound of 0009, as 0109 kept it
KEPT_CHARS = 2000  # going back: 2,000 characters are at most 8,000 bytes of UTF-8


def _bound(most: int) -> None:
    op.drop_constraint(CHECK, "run_inbox", type_="check")
    op.create_check_constraint(CHECK, "run_inbox", sa.func.octet_length(sa.column("body")).between(0, most))


def upgrade() -> None:
    _bound(MAX_INBOX_BYTES)


def downgrade() -> None:
    inbox = sa.table("run_inbox", sa.column("body"))
    longer = sa.func.octet_length(inbox.c.body) > OLD_MAX_BYTES
    op.execute(sa.update(inbox).where(longer).values(body=sa.func.left(inbox.c.body, KEPT_CHARS)))
    _bound(OLD_MAX_BYTES)
