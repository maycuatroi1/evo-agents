"""DLP policies: a project's data loss prevention policy, and the counts of what it redacted or blocked.

Revision ID: 0264
Revises: 0263
Create Date: 2026-11-21

Plan evo-creative-workspace, step 2, criterion a3. dlp_policies holds one policy a project (``evo_agents.hub.dlp``):
its mode, off, redact or block, the rule kinds it applies, and the confidential-contract keywords it adds to the
built-in ones. The hub's gateway route and the hub runtime apply it before a model call leaves for a provider.
dlp_counts counts what it redacted or blocked, by rule kind, surface and action: numbers alone, never a matched value.
The kinds below are copies, never imports, so a later change takes a new revision.

Both tables start empty, and no project has a policy until its admin sets one. Going back drops them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0264"
down_revision = "0263"
branch_labels = None
depends_on = None

MODES = ("off", "redact", "block")  # evo_agents.hub.dlp.MODES
KINDS = ("cif", "national_id", "account_number", "financial_figure", "contract_keyword")  # evo_agents.hub.dlp.KINDS
SURFACES = ("gateway_proxy", "hub_runtime")  # evo_agents.hub.dlp.SURFACES
ACTIONS = ("redacted", "blocked")  # evo_agents.hub.dlp.ACTIONS
MAX_KEYWORDS = 50  # evo_agents.hub.dlp.MAX_KEYWORDS

col = sa.column


def _texts(name: str):
    return sa.column(name, postgresql.ARRAY(sa.Text()))


def upgrade() -> None:
    op.create_table(
        "dlp_policies",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("kinds", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("keywords", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("project_id", name="dlp_policies_pkey"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="dlp_policies_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL", name="dlp_policies_updated_by_fkey"),
        sa.CheckConstraint(
            sa.and_(
                col("mode").in_(MODES),
                _texts("kinds").contained_by(postgresql.array(KINDS, type_=sa.Text())),
                sa.func.cardinality(_texts("keywords")) <= MAX_KEYWORDS,
            ),
            name="dlp_policies_check",
        ),
    )
    op.create_table(
        "dlp_counts",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "kind", "surface", "action", name="dlp_counts_pkey"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="dlp_counts_project_id_fkey"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(KINDS),
                col("surface").in_(SURFACES),
                col("action").in_(ACTIONS),
                col("count") >= 0,
            ),
            name="dlp_counts_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("dlp_counts")
    op.drop_table("dlp_policies")
