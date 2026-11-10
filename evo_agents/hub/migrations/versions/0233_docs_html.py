"""The format of a version of a document of the feature docs: docs_versions.format.

Revision ID: 0233
Revises: 0232
Create Date: 2026-11-10

A version's body is Markdown, as every version before, or an HTML page, which the hub serves from its own origin in a
sandbox (``evo_agents.hub.server.docs``). The column defaults to ``markdown``, so the code before it runs on the
schema after it and every existing version keeps its meaning. The check holds the two formats of
``evo_agents.hub.tables_docs.FORMATS`` as of this revision, copied here, never imported.

Going back drops the column; a version saved as HTML then reads as Markdown text.
"""

import sqlalchemy as sa
from alembic import op

revision = "0233"
down_revision = "0232"
branch_labels = None
depends_on = None

FORMATS = ("markdown", "html")


def upgrade() -> None:
    op.add_column("docs_versions", sa.Column("format", sa.Text(), server_default="markdown", nullable=False))
    op.create_check_constraint("docs_versions_format_check", "docs_versions", sa.column("format").in_(FORMATS))


def downgrade() -> None:
    op.drop_constraint("docs_versions_format_check", "docs_versions", type_="check")
    op.drop_column("docs_versions", "format")
