"""The Curator's changes of knowledge.yaml sources: the kind kg_source and what a pull request does to knowledge.yaml.

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-20

``evo_agents.hub.tiers``, ``evo_agents.hub.knowledge_diff`` and ``evo_agents.hub.server.change_tiers`` describe the
design; the list below is a copy of ``tiers.CHANGE_KINDS`` as of this revision, never an import, so a later change to it
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions, as
``evo_agents.hub.tables_curator`` describes the tables.

proposals.kind takes a twenty-second value, 'kg_source': the ref, fetch, refresh, include, exclude or allow of a git or
harness source already in knowledge.yaml, tier 0 while the pull request's diff, read key by key, edits nothing else.

curator_changes.knowledge holds, for a change of kind kg_source whose pull request changes a knowledge.yaml, what the
hub read of each such file at the pull request's merge base and its head: an object of ``repo:path``, each the sources
the change edits, the edits and what else it changes (``KnowledgeDiff.to_json``), or null for a file the hub could not
read; NULL for any other change. A judge run of the change runs the checks it names.

Going back deletes the proposals of kind kg_source (those dropped as repeats of one first; their changes, ledger lines
and notifications go with them), drops the column and puts back the check of 0018, which is the schema that schema 0053
runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

CHANGE_KINDS_0053 = (
    "docs",
    "memory",
    "test_add",
    "fix",
    "refactor",
    "lint",
    "skill",
    "cli",
    "release_prep",
    "feature",
    "api_change",
    "schema_change",
    "global_config",
    "dependency_major",
    "operation",
    "test_loosen",
    "verify_change",
    "ci_change",
    "credentials",
    "curator_rules",
    "revert",
)
NEW_KINDS = ("kg_source",)
CHANGE_KINDS = (*CHANGE_KINDS_0053, *NEW_KINDS)  # evo_agents.hub.tiers.CHANGE_KINDS

col = sa.column


def _proposal_kinds(kinds) -> None:
    op.create_check_constraint("proposals_kind_check", "proposals", col("kind").in_(kinds))


def upgrade() -> None:
    op.drop_constraint("proposals_kind_check", "proposals", type_="check")
    _proposal_kinds(CHANGE_KINDS)
    op.add_column("curator_changes", sa.Column("knowledge", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "curator_changes_knowledge_check", "curator_changes", sa.func.jsonb_typeof(col("knowledge")) == "object"
    )


def downgrade() -> None:
    proposals = sa.table("proposals", col("id"), col("kind"), col("duplicate_of"))
    new = sa.select(proposals.c.id).where(proposals.c.kind.in_(NEW_KINDS))
    op.execute(proposals.delete().where(proposals.c.duplicate_of.in_(new)))  # dropped as repeats of one
    op.execute(proposals.delete().where(proposals.c.kind.in_(NEW_KINDS)))
    op.drop_constraint("curator_changes_knowledge_check", "curator_changes", type_="check")
    op.drop_column("curator_changes", "knowledge")
    op.drop_constraint("proposals_kind_check", "proposals", type_="check")
    _proposal_kinds(CHANGE_KINDS_0053)
