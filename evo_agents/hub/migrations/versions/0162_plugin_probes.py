"""The probes of a core plugin's own rules: source plugin, with no proposal or change (plan editorial-on-hub, step 3).

Revision ID: 0162
Revises: 0161
Create Date: 2026-10-26

The names below are copies of ``evo_agents.hub.tables_curator``, ``evo_agents.hub.probes`` and ``evo_agents.hub.runs``
as of this revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic
operation on SQLAlchemy Core expressions.

curator_probes takes the source ``plugin``: a probe a core plugin added for a rule of its own, which names the plugin
(plugin_id), the rule's key (ref, 1 to MAX_REF_CHARS characters) and the files each arm places (arms, a JSON object:
{"on": {path: text}, "off": {path: text}}), and no proposal or change; a probe of any other source names a proposal
or a change, as 0130 asked, and none of the three. curator_probes_plugin_ref_key keeps one active probe per project,
plugin and ref. curator_probe_runs.change_id may be NULL: the week of a plugin's probe has no change. A probe run may
name a plan or none (runs_kind_plan_check): a run of plugin probes has no plan.

Going back deletes the probe runs that name no plan, the probe weeks with no change and the plugin probes, puts the
checks of 0130 and 0131 back, and drops the index and the three columns.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0162"
down_revision = "0161"
branch_labels = None
depends_on = None

STATES = ("active", "retired")  # probes.STATES
SOURCES_0130 = ("proposal", "review", "admin")
PLUGIN = "plugin"
SOURCES = (*SOURCES_0130, PLUGIN)  # probes.SOURCES
MAX_TASK_BYTES = 16 * 1024
MAX_VERIFY_CHARS = 2000
MAX_NOTE_CHARS = 2000
MAX_REPO_CHARS = 200
MAX_REF_CHARS = 200  # probes.MAX_REF_CHARS
PROBE_CHECK = "curator_probes_check"
PLAN_CHECK = "runs_kind_plan_check"
KIND = "probe"
ANY_PLAN_0081 = ("author", "local", "judge")  # the kinds that may name a plan or none, as 0131 left them
NO_PLAN = ("review", "chat", "task", "history", "release", "question", "browser")  # as 0131 left them

col = sa.column


def _probe_check(sources, origin) -> None:
    """The check of curator_probes as 0130 wrote it, its source one of ``sources`` and ``origin`` what it names."""
    op.drop_constraint(PROBE_CHECK, "curator_probes", type_="check")
    op.create_check_constraint(
        PROBE_CHECK,
        "curator_probes",
        sa.and_(
            col("state").in_(STATES),
            col("source").in_(sources),
            sa.func.length(col("repo")).between(1, MAX_REPO_CHARS),
            sa.func.octet_length(col("task")).between(1, MAX_TASK_BYTES),
            sa.func.length(col("verify")).between(1, MAX_VERIFY_CHARS),
            col("base_sha").is_(None) | col("base_sha").regexp_match("^[0-9a-f]{7,64}$"),
            sa.func.coalesce(sa.func.length(col("retired_note")), 0) <= MAX_NOTE_CHARS,
            origin,
        ),
    )


def _plan_check(any_plan) -> None:
    kind = col("kind")
    op.drop_constraint(PLAN_CHECK, "runs", type_="check")
    op.create_check_constraint(
        PLAN_CHECK, "runs", sa.or_(kind.in_(any_plan), kind.in_(NO_PLAN) == col("plan_id").is_(None))
    )


def upgrade() -> None:
    op.add_column("curator_probes", sa.Column("plugin_id", sa.BigInteger(), nullable=True))
    op.add_column("curator_probes", sa.Column("ref", sa.Text(), nullable=True))
    op.add_column("curator_probes", sa.Column("arms", postgresql.JSONB(), nullable=True))
    op.create_foreign_key(
        "curator_probes_plugin_id_fkey", "curator_probes", "plugins", ["plugin_id"], ["id"], ondelete="CASCADE"
    )
    named = col("proposal_id").isnot(None) | col("change_id").isnot(None)
    plugin = sa.and_(
        col("source") == PLUGIN,
        col("plugin_id").isnot(None),
        sa.func.length(col("ref")).between(1, MAX_REF_CHARS),
        sa.func.jsonb_typeof(col("arms")) == "object",
        col("proposal_id").is_(None),
        col("change_id").is_(None),
    )
    other = sa.and_(
        col("source") != PLUGIN,
        named,
        col("plugin_id").is_(None),
        col("ref").is_(None),
        col("arms").is_(None),
    )
    _probe_check(SOURCES, sa.or_(plugin, other))
    op.create_index(
        "curator_probes_plugin_ref_key",
        "curator_probes",
        ["project_id", "plugin_id", "ref"],
        unique=True,
        postgresql_where=col("plugin_id").isnot(None) & (col("state") == "active"),
    )
    op.alter_column("curator_probe_runs", "change_id", existing_type=sa.BigInteger(), nullable=True)
    _plan_check((*ANY_PLAN_0081, KIND))


def downgrade() -> None:
    runs = sa.table("runs", col("kind"), col("attempt"), col("plan_id"))
    for attempt in (3, 2, 1):  # a retry names the attempt before it: delete the retries first
        op.execute(runs.delete().where(runs.c.kind == KIND, runs.c.plan_id.is_(None), runs.c.attempt == attempt))
    _plan_check(ANY_PLAN_0081)
    probe_runs = sa.table("curator_probe_runs", col("change_id"))
    op.execute(probe_runs.delete().where(probe_runs.c.change_id.is_(None)))
    op.alter_column("curator_probe_runs", "change_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_index("curator_probes_plugin_ref_key", table_name="curator_probes")
    probes = sa.table("curator_probes", col("source"))
    op.execute(probes.delete().where(probes.c.source == PLUGIN))
    _probe_check(SOURCES_0130, col("proposal_id").isnot(None) | col("change_id").isnot(None))
    op.drop_constraint("curator_probes_plugin_id_fkey", "curator_probes", type_="foreignkey")
    op.drop_column("curator_probes", "arms")
    op.drop_column("curator_probes", "ref")
    op.drop_column("curator_probes", "plugin_id")
