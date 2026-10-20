"""Account OAuth: the sealed set of tokens of an account that signed in with OAuth.

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-20

``evo-agents hub account oauth-login`` puts the tokens Claude's OAuth gave a member on the hub: an access token, a
refresh token, when the access token ends and who the account is. The hub keeps all of them as one sealed JSON object
(``evo_agents.hub.server.account_oauth``), in the columns oauth_sealed (the ciphertext with its tag), oauth_nonce and
oauth_key_id of the account's row, as secrets keep a value; no column holds a token in plaintext. An account with no
sealed set is an account of a token of ``claude setup-token``, or of any other env secret, which is every account after
this revision: nothing that exists changes meaning. The three columns are all set or all null.

Going back drops the three columns and the tokens with them: an OAuth account keeps the access token its secret holds,
which is the schema that schema 0063 runs on.
"""

import sqlalchemy as sa
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

col = sa.column


def upgrade() -> None:
    op.add_column("accounts", sa.Column("oauth_sealed", sa.LargeBinary(), nullable=True))
    op.add_column("accounts", sa.Column("oauth_nonce", sa.LargeBinary(), nullable=True))
    op.add_column("accounts", sa.Column("oauth_key_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "accounts_oauth_check",
        "accounts",
        sa.or_(
            sa.and_(col("oauth_sealed").is_(None), col("oauth_nonce").is_(None), col("oauth_key_id").is_(None)),
            sa.and_(
                col("oauth_sealed").is_not(None), col("oauth_nonce").is_not(None), col("oauth_key_id").is_not(None)
            ),
        ),
    )


def downgrade() -> None:
    op.drop_constraint("accounts_oauth_check", "accounts", type_="check")
    op.drop_column("accounts", "oauth_key_id")
    op.drop_column("accounts", "oauth_nonce")
    op.drop_column("accounts", "oauth_sealed")
