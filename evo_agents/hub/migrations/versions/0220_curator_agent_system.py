"""The Curator's lens agent_system: its lens, its kind of change and the change a proposal of it carries.

Revision ID: 0220
Revises: 0219
Create Date: 2026-11-07

A review run that looks through the lens ``agent_system`` (plan multi-agent-systems, criterion a8;
``evo_agents.hub.server.system_curation``) proposes a change of an agent or a named knowledge graph that the curator
node of an agent system optimizes: an agent's instructions, skill or model, or a graph's sources or a decision on it.
Accepting the proposal applies it through the hub, never by hand: a new version of the profile, with a new revision of
the system's spec that pins it, or a change of the graph.

findings.lens and proposals.lens take ``agent_system``, and proposals.kind takes ``agent_config``: the lists below are
copies of ``evo_agents.hub.review.LENSES`` and ``evo_agents.hub.tiers.CHANGE_KINDS`` as of this revision, never
imports, so a later change to them takes a new revision. proposals.system_change holds, for a proposal of that kind,
the change in place of a draft plan (an object), and what accepting it made; NULL for any other proposal.

Going back deletes the findings and the proposals of the lens and of the kind (those dropped as repeats of one first),
drops the column and puts the checks of 0212 and 0057 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0220"
down_revision = "0219"
branch_labels = None
depends_on = None

LENSES_0212 = (
    "tool_errors",
    "environment",
    "corrections",
    "failed_runs",
    "tech_debt",
    "code_health",
    "docs_drift",
    "skills_memory",
    "cost",
    "security",
    "product_goals",
    "kg_health",
    "runtime",
    "harness",
    "product",
    "kg_init",
    "kg_questions",
    "kg_entity_map",
)
NEW_LENSES = ("agent_system",)
LENSES = (*LENSES_0212, *NEW_LENSES)
CHANGE_KINDS_0057 = (
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
    "kg_source",
)
NEW_KINDS = ("agent_config",)
CHANGE_KINDS = (*CHANGE_KINDS_0057, *NEW_KINDS)

col = sa.column


def _checks(lenses, kinds) -> None:
    for table in ("findings", "proposals"):
        op.drop_constraint(f"{table}_lens_check", table, type_="check")
        op.create_check_constraint(f"{table}_lens_check", table, col("lens").in_(lenses))
    op.drop_constraint("proposals_kind_check", "proposals", type_="check")
    op.create_check_constraint("proposals_kind_check", "proposals", col("kind").in_(kinds))


def upgrade() -> None:
    _checks(LENSES, CHANGE_KINDS)
    op.add_column("proposals", sa.Column("system_change", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "proposals_system_change_check", "proposals", sa.func.jsonb_typeof(col("system_change")) == "object"
    )


def downgrade() -> None:
    findings = sa.table("findings", col("lens"))
    proposals = sa.table("proposals", col("id"), col("lens"), col("kind"), col("duplicate_of"))
    new = (proposals.c.lens.in_(NEW_LENSES)) | (proposals.c.kind.in_(NEW_KINDS))
    op.execute(proposals.delete().where(proposals.c.duplicate_of.in_(sa.select(proposals.c.id).where(new))))
    op.execute(proposals.delete().where(new))
    op.execute(findings.delete().where(findings.c.lens.in_(NEW_LENSES)))
    op.drop_constraint("proposals_system_change_check", "proposals", type_="check")
    op.drop_column("proposals", "system_change")
    _checks(LENSES_0212, CHANGE_KINDS_0057)
