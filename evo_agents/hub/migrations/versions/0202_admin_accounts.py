"""Hub admins by GitHub account, and where a market order came from.

Revision ID: 0202
Revises: 0201
Create Date: 2026-11-04

users gains ``admin_login`` (plan code-quality-upgrade, step 2): the login of EVO_HUB_ADMINS bound to the account the
first time it signs in with it (``evo_agents.hub.server.security.bind_admin``), so the account stays an admin after a
rename on GitHub and an account that takes the login after it does not become one. It is null for every existing row
until the hub binds the listed logins at its start; one account holds a login (unique index ``users_admin_login_key``).

market_orders gains ``buyer_address``: the address an order came from (``workers.client_address``), which the open-order
limit counts by instead of the hub id the caller names. It is null for the orders placed before, indexed with the state.

Going back drops both columns and their indexes: the admins are by login again, and the limit by hub id.
"""

import sqlalchemy as sa
from alembic import op

revision = "0202"
down_revision = "0201"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("admin_login", sa.Text(), nullable=True))
    op.create_index("users_admin_login_key", "users", ["admin_login"], unique=True)
    op.add_column("market_orders", sa.Column("buyer_address", sa.Text(), nullable=True))
    op.create_index("market_orders_address_idx", "market_orders", ["buyer_address", "state"])


def downgrade() -> None:
    op.drop_index("market_orders_address_idx", table_name="market_orders")
    op.drop_column("market_orders", "buyer_address")
    op.drop_index("users_admin_login_key", table_name="users")
    op.drop_column("users", "admin_login")
