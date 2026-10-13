"""The skills a run's Skill tool loaded, next to the tool's figures.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-13

run_tool_stats.skills holds, on the row of Claude Code's Skill tool, what its calls loaded: a JSON array of
``{"skill": NAME, "calls": N, "errors": N}``, the most called first (``evo_agents.hub.server.tool_stats.summarize``,
the name from the call's ``rawInput.skill``). NULL on every other row, and on the rows written before this revision:
runs that ended before it have no skills recorded, and the hub does not count them again. A check keeps it an array.
The statements are Alembic operations on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the
table.

Going back drops the column and its check, which is the schema that schema 0025 runs on: its figures do not name
skills.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

CHECK = "run_tool_stats_skills_check"


def upgrade() -> None:
    op.add_column("run_tool_stats", sa.Column("skills", postgresql.JSONB(), nullable=True))
    skills = sa.column("skills", postgresql.JSONB())
    op.create_check_constraint(
        CHECK, "run_tool_stats", sa.or_(skills.is_(None), sa.func.jsonb_typeof(skills) == "array")
    )


def downgrade() -> None:
    op.drop_constraint(CHECK, "run_tool_stats", type_="check")
    op.drop_column("run_tool_stats", "skills")
