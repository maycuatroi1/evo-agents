"""The Curator's ledger, the revert the hub proposes, and the circuit breaker of the night shift.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-08

``evo_agents.hub.ledger``, ``evo_agents.hub.server.ledger`` and ``evo_agents.hub.server.outcomes`` describe the design;
the lists and bounds below are copies of their constants as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions, as
``evo_agents.hub.tables`` describes the tables.

curator_ledger holds the lines of each proposal's ledger, which the hub only adds to: what happened (action), who did
it (actor: the hub's own code, an agent of a run of the Curator with its run, or a member), in words (what), and when
it applies the change, the commit, the commits of the default branch before and after, the figures (those that set
the proposal off, or its outcome), the Judge's verdict, the pull request, when it merged, the outcome of a merged
change (one per change, curator_ledger_outcome_key) and other details. A line goes with its proposal and its project.

proposals.kind takes a twenty-first value, 'revert', and proposals.revert_of names the proposal whose merged change a
revert proposal undoes (only a revert names one).

schedules.pause_reason says why the hub paused a schedule (its circuit breaker), which it does without a member:
paused_at is set while it is paused, with paused_by (a member paused it) or pause_reason (the hub did), and both are
NULL while it runs.

notifications.notice_kind takes a sixth value, 'curator_paused': the circuit breaker paused the night shift.

Going back drops the ledger, deletes the revert proposals and the notices curator_paused, keeps a schedule the hub
paused paused as its owner's pause, drops the two columns and puts the checks of 0017 back, which is the schema that
schema 0017 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

NOTICE_KINDS_0017 = ("push_default_branch", "merge_default_branch", "plan_finished", "run_failed", "curator_brief")
NOTICE_KINDS = (*NOTICE_KINDS_0017, "curator_paused")  # evo_agents.hub.runs.NOTICE_KINDS
CHANGE_KINDS_0017 = (
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
)
CHANGE_KINDS = (*CHANGE_KINDS_0017, "revert")  # evo_agents.hub.tiers.CHANGE_KINDS
ACTORS = ("curator", "agent", "user")  # evo_agents.hub.ledger.ACTORS
ACTIONS = (  # evo_agents.hub.ledger.ACTIONS
    "proposed",
    "dropped",
    "accepted",
    "rejected",
    "deferred",
    "planned",
    "built",
    "build_failed",
    "pull_opened",
    "judged",
    "merged",
    "left_open",
    "closed",
    "outcome",
)
OUTCOMES = ("keep", "revert", "unclear")  # evo_agents.hub.ledger.OUTCOMES
MAX_WHAT_CHARS = 2000  # evo_agents.hub.ledger.MAX_WHAT_CHARS
MAX_REASON_CHARS = 2000
MAX_URL_CHARS = 2000
SHA = r"^[0-9a-f]{40}([0-9a-f]{24})?$"

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _line(name: str, most: int):
    """1 to ``most`` characters of ``name``, without a control character."""
    return sa.and_(sa.func.char_length(col(name)).between(1, most), ~col(name).op("~")(r"[\x01-\x1f\x7f]"))


def _object(name: str):
    return sa.func.jsonb_typeof(col(name)) == "object"


def _notice_kinds(kinds) -> None:
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(kinds))


def _proposal_kinds(kinds) -> None:
    op.create_check_constraint("proposals_kind_check", "proposals", col("kind").in_(kinds))


def upgrade() -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    _notice_kinds(NOTICE_KINDS)

    # proposals: the revert the hub proposes
    op.drop_constraint("proposals_kind_check", "proposals", type_="check")
    _proposal_kinds(CHANGE_KINDS)
    op.add_column("proposals", sa.Column("revert_of", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "proposals_revert_of_fkey", "proposals", "proposals", ["revert_of"], ["id"], ondelete="SET NULL"
    )
    op.create_check_constraint(
        "proposals_revert_of_check", "proposals", sa.or_(col("revert_of").is_(None), col("kind") == "revert")
    )

    # schedules: paused by the hub's circuit breaker, with why
    op.add_column("schedules", sa.Column("pause_reason", sa.Text(), nullable=True))
    op.create_check_constraint("schedules_pause_reason_check", "schedules", _line("pause_reason", MAX_REASON_CHARS))
    op.drop_constraint("schedules_paused_check", "schedules", type_="check")
    op.create_check_constraint(
        "schedules_paused_check",
        "schedules",
        sa.or_(
            sa.and_(col("paused_at").is_(None), col("paused_by").is_(None), col("pause_reason").is_(None)),
            sa.and_(
                col("paused_at").is_not(None), sa.or_(col("paused_by").is_not(None), col("pause_reason").is_not(None))
            ),
        ),
    )

    # the ledger
    op.create_table(
        "curator_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("change_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("what", sa.Text(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("before_sha", sa.Text(), nullable=True),
        sa.Column("after_sha", sa.Text(), nullable=True),
        sa.Column("figures", postgresql.JSONB(), nullable=True),
        sa.Column("verdict", postgresql.JSONB(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        _stamp("created_at"),
        sa.CheckConstraint(col("action").in_(ACTIONS), name="curator_ledger_action_check"),
        sa.CheckConstraint(col("actor").in_(ACTORS), name="curator_ledger_actor_check"),
        sa.CheckConstraint(
            sa.or_(col("actor_id").is_(None), col("actor") == "user"), name="curator_ledger_actor_id_check"
        ),
        sa.CheckConstraint(
            sa.or_(col("actor") != "agent", col("run_id").is_not(None)), name="curator_ledger_run_check"
        ),
        sa.CheckConstraint(_line("what", MAX_WHAT_CHARS), name="curator_ledger_what_check"),
        sa.CheckConstraint(col("commit_sha").op("~")(SHA), name="curator_ledger_commit_sha_check"),
        sa.CheckConstraint(col("before_sha").op("~")(SHA), name="curator_ledger_before_sha_check"),
        sa.CheckConstraint(col("after_sha").op("~")(SHA), name="curator_ledger_after_sha_check"),
        sa.CheckConstraint(_object("figures"), name="curator_ledger_figures_check"),
        sa.CheckConstraint(_object("verdict"), name="curator_ledger_verdict_check"),
        sa.CheckConstraint(_object("details"), name="curator_ledger_details_check"),
        sa.CheckConstraint(_line("pr_url", MAX_URL_CHARS), name="curator_ledger_pr_url_check"),
        sa.CheckConstraint(col("pr_number") >= 1, name="curator_ledger_pr_number_check"),
        sa.CheckConstraint(col("outcome").in_(OUTCOMES), name="curator_ledger_outcome_check"),
        sa.CheckConstraint(
            (col("action") == "outcome") == col("outcome").is_not(None), name="curator_ledger_outcome_action_check"
        ),
        sa.CheckConstraint(
            sa.or_(col("action") != "outcome", col("change_id").is_not(None)),
            name="curator_ledger_outcome_change_check",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT", name="curator_ledger_actor_id_fkey"),
        sa.ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="CASCADE", name="curator_ledger_change_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_ledger_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], ondelete="CASCADE", name="curator_ledger_proposal_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_ledger_run_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="curator_ledger_pkey"),
    )
    op.create_index("curator_ledger_proposal_idx", "curator_ledger", ["proposal_id", "id"])
    op.create_index("curator_ledger_project_idx", "curator_ledger", ["project_id", "created_at"])
    op.create_index(
        "curator_ledger_outcome_key",
        "curator_ledger",
        ["change_id"],
        unique=True,
        postgresql_where=col("action") == "outcome",
    )


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    proposals = sa.table("proposals", col("id"), col("kind"), col("duplicate_of"))
    schedules = sa.table("schedules", col("paused_at"), col("paused_by"), col("owner_id"))
    op.drop_index("curator_ledger_outcome_key", table_name="curator_ledger")
    op.drop_index("curator_ledger_project_idx", table_name="curator_ledger")
    op.drop_index("curator_ledger_proposal_idx", table_name="curator_ledger")
    op.drop_table("curator_ledger")

    op.drop_constraint("schedules_paused_check", "schedules", type_="check")
    op.execute(
        schedules.update()
        .values(paused_by=schedules.c.owner_id)
        .where(schedules.c.paused_at.is_not(None), schedules.c.paused_by.is_(None))
    )
    op.create_check_constraint(
        "schedules_paused_check", "schedules", col("paused_at").is_(None) == col("paused_by").is_(None)
    )
    op.drop_constraint("schedules_pause_reason_check", "schedules", type_="check")
    op.drop_column("schedules", "pause_reason")

    op.drop_constraint("proposals_revert_of_check", "proposals", type_="check")
    op.drop_constraint("proposals_revert_of_fkey", "proposals", type_="foreignkey")
    op.drop_column("proposals", "revert_of")
    reverts = sa.select(proposals.c.id).where(proposals.c.kind == "revert")
    op.execute(proposals.delete().where(proposals.c.duplicate_of.in_(reverts)))  # dropped as repeats of one
    op.execute(proposals.delete().where(proposals.c.kind == "revert"))
    op.drop_constraint("proposals_kind_check", "proposals", type_="check")
    _proposal_kinds(CHANGE_KINDS_0017)

    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.execute(notifications.delete().where(notifications.c.notice_kind == "curator_paused"))
    _notice_kinds(NOTICE_KINDS_0017)
