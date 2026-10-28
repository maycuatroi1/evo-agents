"""A release cycle has no cost cap.

Revision ID: 0176
Revises: 0175
Create Date: 2026-10-28

release_schedules loses cycle_budget_usd, the cap of one cycle in USD, and release_cycles loses budget_usd, the cap of
the cycle, which its owner's ``continue`` raised by the schedule's cap. A cycle now stops only on its other causes;
``budget`` stays among the stop causes, since cycles before this revision stopped on it. The checks of both tables name
the column, so each is put back as 0067 made it without that term. The values below are copies of 0067's, never
imports.

Going back adds both columns again with DEFAULT_CYCLE_USD on every row, and the checks of 0067.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0176"
down_revision = "0175"
branch_labels = None
depends_on = None

CYCLE_STATES = ("open", "stopped", "published", "closed")
ENDED = ("published", "closed")
RUN_MODES = ("cut", "fix")
AT = "^([01][0-9]|2[0-3]):[0-5][0-9]$"
TIME_ZONE = "^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$"
TAG_PATTERN = r"^[A-Za-z0-9._/-]{0,50}\*$"
REF = "^[A-Za-z0-9._/-]{1,200}$"
WORKFLOW = r"^[A-Za-z0-9._-]{1,100}\.ya?ml$"
OBJECT_NAME = "^([0-9a-f]{40}|[0-9a-f]{64})$"
CYCLE_USD = (1.0, 10_000.0)
DEFAULT_CYCLE_USD = 200.0
MAX_PATHS = 50
MAX_REASON_CHARS = 2000
MAX_TAG_CHARS = 200
CONTROL = r"[\x01-\x1f\x7f]"
NOT_A_TEXT = '$[*] ? (@.type() != "string")'
STOP_CAUSES = ("run_failed", "run_cancelled", "refused", "github", "hacking", "budget", "same_test", "timeout", "ci")
SCHEDULES, CYCLES = "release_schedules", "release_cycles"

col = sa.column


def _text(name: str, limit: int):
    value = col(name)
    return sa.or_(value.is_(None), sa.and_(sa.func.char_length(value).between(1, limit), ~value.op("~")(CONTROL)))


def _commit(name: str):
    return sa.or_(col(name).is_(None), col(name).regexp_match(OBJECT_NAME))


def _paths(name: str, *, least: int):
    value = col(name)
    texts_only = sa.cast(sa.literal(NOT_A_TEXT), postgresql.JSONPATH)
    return sa.case(
        (
            sa.func.jsonb_typeof(value) == "array",
            sa.and_(
                sa.func.jsonb_array_length(value).between(least, MAX_PATHS),
                ~sa.func.jsonb_path_exists(value, texts_only),
            ),
        ),
        else_=False,
    )


def _schedule_rule(capped: bool):
    terms = [
        _text("repo", 200),
        col("check_at").regexp_match(AT),
        sa.func.char_length(col("timezone")) <= 64,
        col("timezone").regexp_match(TIME_ZONE),
        col("target_ref").regexp_match(REF),
        col("tag_pattern").regexp_match(TAG_PATTERN),
        col("workflow").regexp_match(WORKFLOW),
        _paths("paths", least=1),
        *([col("cycle_budget_usd").between(*CYCLE_USD)] if capped else []),
        (col("paused_at").is_(None)) == (col("paused_by").is_(None)),
        col("updated_at") >= col("created_at"),
    ]
    return sa.and_(*terms)


def _cycle_rule(capped: bool):
    terms = [
        col("state").in_(CYCLE_STATES),
        col("start").in_(RUN_MODES),
        _text("base_tag", MAX_TAG_CHARS),
        _commit("base_commit"),
        col("target_ref").regexp_match(REF),
        _commit("target_commit"),
        _paths("changed_paths", least=0),
        *([col("budget_usd") > 0] if capped else []),
        _text("stop_reason", MAX_REASON_CHARS),
        sa.or_(col("span").is_(None), sa.func.jsonb_typeof(col("span")) == "object"),
        col("state").in_(ENDED) == col("ended_at").is_not(None),
        sa.or_(col("stop_cause").is_(None), col("stop_cause").in_(STOP_CAUSES)),
    ]
    return sa.and_(*terms)


def upgrade() -> None:
    for table, column, rule in ((SCHEDULES, "cycle_budget_usd", _schedule_rule), (CYCLES, "budget_usd", _cycle_rule)):
        op.drop_constraint(f"{table}_check", table, type_="check")
        op.drop_column(table, column)
        op.create_check_constraint(f"{table}_check", table, rule(False))


def downgrade() -> None:
    for table, column, rule in ((SCHEDULES, "cycle_budget_usd", _schedule_rule), (CYCLES, "budget_usd", _cycle_rule)):
        op.drop_constraint(f"{table}_check", table, type_="check")
        op.add_column(table, sa.Column(column, sa.Double(), server_default=str(DEFAULT_CYCLE_USD), nullable=False))
        op.alter_column(table, column, server_default=None)
        op.create_check_constraint(f"{table}_check", table, rule(True))
