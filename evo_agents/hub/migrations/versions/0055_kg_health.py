"""The health of a project's knowledge graph over time, and the Curator's lens on it.

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-19

``evo_agents.hub.kg_coverage`` holds the model and ``evo_agents.hub.server.kg_health`` the measure; the values below are
copies of their constants and of ``evo_agents.hub.review.LENSES`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions, as
``evo_agents.hub.tables_kg`` describes the tables.

kg_builds.coverage is what a successful build says of the graph it made: per source its items and mentions, how many
of them resolved, stayed ambiguous or dangled, and the commit a git source read; the Python functions and methods no
call edge reaches; an object, NULL for a build of an older schema or one that failed. kg_source_files holds, per git
source of a project, the files of the project's latest successful build: each path with its revision, the blob SHA,
replaced by every build that succeeds. kg_health holds each measure of a project's graph the hub took, an object:
per git source the commit read against the default branch on the forge now (how many commits behind, since when, the
files the graph lacks, holds at an older revision or holds though the branch deleted them), and the retrieval
evaluation of the graph's latest build. findings.lens and proposals.lens take ``kg_health``.

Going back deletes the findings and the proposals of lens kg_health (and those dropped as repeats of one), puts the
checks of 0054 back, and drops the two tables and the column: schema 0054 keeps no health of a graph.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

LENSES_0054 = (
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
)
NEW_LENSES = ("kg_health",)
LENSES = (*LENSES_0054, *NEW_LENSES)  # evo_agents.hub.review.LENSES
MAX_SOURCE_CHARS = 200
MAX_ERROR_CHARS = 2000  # evo_agents.hub.server.kg_health.ERROR_CHARS

col = sa.column


def _object(name: str):
    return sa.func.jsonb_typeof(col(name)) == "object"


def _lenses(lenses) -> None:
    for table in ("findings", "proposals"):
        op.create_check_constraint(f"{table}_lens_check", table, col("lens").in_(lenses))


def _drop_lenses() -> None:
    for table in ("findings", "proposals"):
        op.drop_constraint(f"{table}_lens_check", table, type_="check")


def upgrade() -> None:
    op.add_column("kg_builds", sa.Column("coverage", postgresql.JSONB(), nullable=True))
    op.create_check_constraint("kg_builds_coverage_check", "kg_builds", _object("coverage"))
    op.create_table(
        "kg_source_files",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("build_id", sa.BigInteger(), nullable=False),
        sa.Column("read_commit", sa.Text(), nullable=True),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            sa.func.char_length(col("source")).between(1, MAX_SOURCE_CHARS), name="kg_source_files_source_check"
        ),
        sa.CheckConstraint(_object("files"), name="kg_source_files_files_check"),
        sa.CheckConstraint(col("file_count") >= 0, name="kg_source_files_count_check"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_source_files_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["build_id"], ["kg_builds.id"], ondelete="CASCADE", name="kg_source_files_build_id_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "source", name="kg_source_files_pkey"),
    )
    op.create_table(
        "kg_health",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("build_id", sa.BigInteger(), nullable=True),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(_object("body"), name="kg_health_body_check"),
        sa.CheckConstraint(sa.func.char_length(col("error")) <= MAX_ERROR_CHARS, name="kg_health_error_check"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_health_project_id_fkey"),
        sa.ForeignKeyConstraint(["build_id"], ["kg_builds.id"], ondelete="SET NULL", name="kg_health_build_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="kg_health_pkey"),
    )
    op.create_index("kg_health_project_idx", "kg_health", ["project_id", "measured_at"])
    _drop_lenses()
    _lenses(LENSES)


def downgrade() -> None:
    findings = sa.table("findings", col("lens"))
    proposals = sa.table("proposals", col("id"), col("lens"), col("duplicate_of"))
    new = sa.select(proposals.c.id).where(proposals.c.lens.in_(NEW_LENSES))
    op.execute(proposals.delete().where(proposals.c.duplicate_of.in_(new)))  # dropped as repeats of one
    op.execute(proposals.delete().where(proposals.c.lens.in_(NEW_LENSES)))
    op.execute(findings.delete().where(findings.c.lens.in_(NEW_LENSES)))
    _drop_lenses()
    _lenses(LENSES_0054)
    op.drop_index("kg_health_project_idx", table_name="kg_health")
    op.drop_table("kg_health")
    op.drop_table("kg_source_files")
    op.drop_constraint("kg_builds_coverage_check", "kg_builds", type_="check")
    op.drop_column("kg_builds", "coverage")
