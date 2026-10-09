"""The review run of the night shift: a run on no plan, the night's figures, and what the Reviewer finds and proposes.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-08

``evo_agents.hub.review``, ``evo_agents.hub.tiers`` and ``evo_agents.hub.server.proposals`` describe the design; the
lists and bounds below are copies of their constants as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions, as
``evo_agents.hub.tables`` describes the tables.

runs.kind takes a third value, 'review', the Curator's Reviewer of one night of a project's charter. A review run works
on no plan: its plan_id and plan_revision are NULL, exactly for that kind, so the foreign key to plan_revisions does
not apply to it; it has repos as a plan run has, and no step key or repo; it belongs to a schedule; and a project has
at most one review run active at a time (runs_active_review_key). The checks that tied step_key, repo and repos to the
kind 'plan' now tie them to the kind 'step', which says the same of the two kinds there were. A run that is not of one
step may take up to 24 hours of agent time, as a plan run may.

workers.run_kinds lists the kinds of run the worker's daemon says it runs; a daemon that says none (an older one)
takes no review run.

curator_figures holds the figures the job curator.collect counted for one night of a project (a JSON object), the
schedule it counted them for, the span of time they cover, and the review run queued on them.

findings holds what a review run recorded: its lens, its severity, a title, a markdown body, the evidence the hub
resolved (a JSON array of 1 to 50 objects) and the project's label. proposals holds what it proposed: the lens and the
kind of change, the paths it would edit and the ones the knowledge graph says those reach (JSON arrays of {repo, path}),
the tier the hub computed with its reasons, the findings and the evidence that support it and how many pieces they
count, the draft plan (a JSON object), the fingerprint the rule of rejected proposals compares, and the owner's answer:
state open, accepted, rejected, deferred (until deferred_until) or dropped (as the duplicate of a rejected one), who
answered, when, and a note. inbox_at is when a tier 2 proposal became an Inbox item of the owner.

notifications.kind takes a third value, 'proposal', with proposal_id naming the proposal, exactly for that kind.

Going back deletes the review runs and the notifications of proposals, drops the three tables and the columns, and puts
the checks of 0013 back, which is the schema that schema 0013 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

KINDS_0013 = ("step", "plan")
KINDS = ("step", "plan", "review")  # evo_agents.hub.runs.RUN_KINDS
ACTIVE = ("queued", "leased", "running", "interactive", "verifying", "waiting", "review", "parked")
STEP_TIMEOUT = (5 * 60, 240 * 60)  # a run of one step, as in 0010
LONG_TIMEOUT = 24 * 60 * 60  # a run of any other kind, as a plan run in 0010
NOTIFICATION_KINDS_0013 = ("decision", "notice")
NOTIFICATION_KINDS = ("decision", "notice", "proposal")  # evo_agents.hub.runs.NOTIFICATION_KINDS
MAX_RUN_KINDS = 20
LENSES = (  # evo_agents.hub.review.LENSES
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
SEVERITIES = ("low", "medium", "high")  # evo_agents.hub.review.SEVERITIES
CHANGE_KINDS = (  # evo_agents.hub.tiers.CHANGE_KINDS
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
STATES = ("open", "accepted", "rejected", "deferred", "dropped")  # evo_agents.hub.review.PROPOSAL_STATES
ANSWERED = ("accepted", "rejected", "deferred")
MAX_TITLE_CHARS = 200  # evo_agents.hub.review.MAX_TITLE_CHARS
MAX_BODY_BYTES = 16 * 1024  # evo_agents.hub.review.MAX_BODY_BYTES
MAX_EVIDENCE = 50  # evo_agents.hub.review.MAX_EVIDENCE
MAX_PATHS = 100  # evo_agents.hub.tiers.MAX_PATHS
MAX_NOTE_CHARS = 2000  # evo_agents.hub.review.MAX_NOTE_CHARS
MAX_DRAFT_BYTES = 256 * 1024  # evo_agents.hub.review.MAX_DRAFT_BYTES

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _type(name: str, kind: str):
    return sa.func.jsonb_typeof(col(name)) == kind


def _line(name: str, most: int):
    """1 to ``most`` characters of ``name``, without a control character."""
    return sa.and_(sa.func.char_length(col(name)).between(1, most), ~col(name).op("~")(r"[\x01-\x1f\x7f]"))


def _bytes(name: str, most: int):
    return sa.func.octet_length(col(name)).between(1, most)


def _array(name: str, fewest: int, most: int):
    return sa.and_(_type(name, "array"), sa.func.jsonb_array_length(col(name)).between(fewest, most))


def _runs_checks(kinds, step_column: str) -> None:
    """The checks of runs that name the kinds: the kinds themselves, and step_key, repo and repos tied to the kind
    ``step_column`` says (0013: plan; 0014: step)."""
    op.create_check_constraint("runs_kind_check", "runs", col("kind").in_(kinds))
    if step_column == "plan":
        op.create_check_constraint(
            "runs_kind_step_key_check", "runs", (col("kind") == "plan") == col("step_key").is_(None)
        )
        op.create_check_constraint("runs_kind_repo_check", "runs", (col("kind") == "plan") == col("repo").is_(None))
        op.create_check_constraint(
            "runs_kind_repos_check", "runs", (col("kind") == "plan") == col("repos").is_not(None)
        )
        timeout = sa.case((col("kind") == "plan", LONG_TIMEOUT), else_=STEP_TIMEOUT[1])
    else:
        op.create_check_constraint(
            "runs_kind_step_key_check", "runs", (col("kind") == "step") == col("step_key").is_not(None)
        )
        op.create_check_constraint("runs_kind_repo_check", "runs", (col("kind") == "step") == col("repo").is_not(None))
        op.create_check_constraint("runs_kind_repos_check", "runs", (col("kind") == "step") == col("repos").is_(None))
        timeout = sa.case((col("kind") == "step", STEP_TIMEOUT[1]), else_=LONG_TIMEOUT)
    op.create_check_constraint("runs_timeout_s_check", "runs", col("timeout_s").between(STEP_TIMEOUT[0], timeout))


def _drop_runs_checks() -> None:
    for name in ("runs_kind_check", "runs_kind_step_key_check", "runs_kind_repo_check", "runs_kind_repos_check"):
        op.drop_constraint(name, "runs", type_="check")
    op.drop_constraint("runs_timeout_s_check", "runs", type_="check")


def upgrade() -> None:
    # runs: a review run, on no plan
    _drop_runs_checks()
    op.alter_column("runs", "plan_id", existing_type=sa.Text(), nullable=True)
    op.alter_column("runs", "plan_revision", existing_type=sa.Integer(), nullable=True)
    _runs_checks(KINDS, "step")
    op.create_check_constraint("runs_kind_plan_check", "runs", (col("kind") == "review") == col("plan_id").is_(None))
    op.create_check_constraint(
        "runs_plan_revision_check", "runs", col("plan_id").is_(None) == col("plan_revision").is_(None)
    )
    op.create_check_constraint(
        "runs_review_schedule_check", "runs", sa.or_(col("kind") != "review", col("schedule_id").is_not(None))
    )
    op.create_index(
        "runs_active_review_key",
        "runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.and_(col("kind") == "review", col("state").in_(ACTIVE)),
    )
    op.add_column("workers", sa.Column("run_kinds", postgresql.ARRAY(sa.Text()), nullable=True))
    op.create_check_constraint(
        "workers_run_kinds_check", "workers", sa.func.cardinality(col("run_kinds")) <= MAX_RUN_KINDS
    )

    # the night's figures
    op.create_table(
        "curator_figures",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("schedule_id", sa.BigInteger(), nullable=False),
        sa.Column("night", sa.Date(), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("figures", postgresql.JSONB(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        sa.CheckConstraint(_type("figures", "object"), name="curator_figures_figures_check"),
        sa.CheckConstraint(col("until") >= col("since"), name="curator_figures_span_check"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_figures_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_figures_run_id_fkey"),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["schedules.id"], ondelete="CASCADE", name="curator_figures_schedule_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="curator_figures_pkey"),
        sa.UniqueConstraint("project_id", "night", name="curator_figures_project_id_night_key"),
    )

    # findings
    op.create_table(
        "findings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("lens", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        _stamp("created_at"),
        sa.CheckConstraint(col("lens").in_(LENSES), name="findings_lens_check"),
        sa.CheckConstraint(col("severity").in_(SEVERITIES), name="findings_severity_check"),
        sa.CheckConstraint(_line("title", MAX_TITLE_CHARS), name="findings_title_check"),
        sa.CheckConstraint(_bytes("body", MAX_BODY_BYTES), name="findings_body_check"),
        sa.CheckConstraint(_array("evidence", 1, MAX_EVIDENCE), name="findings_evidence_check"),
        sa.CheckConstraint(_type("label", "object"), name="findings_label_check"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="findings_project_id_fkey"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="findings_run_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="findings_pkey"),
    )
    op.create_index("findings_run_idx", "findings", ["run_id", "id"])
    op.create_index("findings_project_idx", "findings", [col("project_id"), col("id").desc()])

    # proposals
    op.create_table(
        "proposals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("lens", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("paths", postgresql.JSONB(), nullable=False),
        sa.Column("impacted", postgresql.JSONB(), nullable=True),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("tier_reasons", postgresql.JSONB(), nullable=False),
        sa.Column("finding_ids", postgresql.ARRAY(sa.BigInteger()), server_default="{}", nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("draft", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="open", nullable=False),
        sa.Column("duplicate_of", sa.BigInteger(), nullable=True),
        sa.Column("answered_by", sa.BigInteger(), nullable=True),
        _when("answered_at"),
        sa.Column("note", sa.Text(), nullable=True),
        _when("deferred_until"),
        _when("inbox_at"),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        _stamp("created_at"),
        sa.CheckConstraint(col("lens").in_(LENSES), name="proposals_lens_check"),
        sa.CheckConstraint(col("kind").in_(CHANGE_KINDS), name="proposals_kind_check"),
        sa.CheckConstraint(_line("title", MAX_TITLE_CHARS), name="proposals_title_check"),
        sa.CheckConstraint(_bytes("summary", MAX_BODY_BYTES), name="proposals_summary_check"),
        sa.CheckConstraint(_array("paths", 0, MAX_PATHS), name="proposals_paths_check"),
        sa.CheckConstraint(_type("impacted", "array"), name="proposals_impacted_check"),
        sa.CheckConstraint(col("tier").between(0, 3), name="proposals_tier_check"),
        sa.CheckConstraint(_array("tier_reasons", 1, 1000), name="proposals_tier_reasons_check"),
        sa.CheckConstraint(_array("evidence", 0, MAX_EVIDENCE), name="proposals_evidence_check"),
        sa.CheckConstraint(col("evidence_count") >= 1, name="proposals_evidence_count_check"),
        sa.CheckConstraint(
            sa.and_(
                _type("draft", "object"), sa.func.octet_length(sa.cast(col("draft"), sa.Text())) <= MAX_DRAFT_BYTES
            ),
            name="proposals_draft_check",
        ),
        sa.CheckConstraint(sa.func.char_length(col("fingerprint")) == 64, name="proposals_fingerprint_check"),
        sa.CheckConstraint(col("state").in_(STATES), name="proposals_state_check"),
        sa.CheckConstraint(
            (col("state") == "dropped") == col("duplicate_of").is_not(None), name="proposals_dropped_check"
        ),
        sa.CheckConstraint(
            (col("state") == "deferred") == col("deferred_until").is_not(None), name="proposals_deferred_check"
        ),
        sa.CheckConstraint(
            col("answered_by").is_(None) == col("answered_at").is_(None), name="proposals_answered_check"
        ),
        sa.CheckConstraint(
            sa.or_(col("state").not_in(ANSWERED), col("answered_at").is_not(None)), name="proposals_answer_check"
        ),
        sa.CheckConstraint(_line("note", MAX_NOTE_CHARS), name="proposals_note_check"),
        sa.CheckConstraint(_type("label", "object"), name="proposals_label_check"),
        sa.ForeignKeyConstraint(["answered_by"], ["users.id"], ondelete="RESTRICT", name="proposals_answered_by_fkey"),
        sa.ForeignKeyConstraint(
            ["duplicate_of"], ["proposals.id"], ondelete="RESTRICT", name="proposals_duplicate_of_fkey"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="proposals_project_id_fkey"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="proposals_run_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="proposals_pkey"),
    )
    op.create_index("proposals_run_idx", "proposals", ["run_id", "id"])
    op.create_index("proposals_project_idx", "proposals", [col("project_id"), col("id").desc()])
    op.create_index(  # the rule of rejected proposals: the latest rejection of a fingerprint
        "proposals_rejected_idx",
        "proposals",
        [col("project_id"), col("fingerprint"), col("answered_at").desc()],
        postgresql_where=col("state") == "rejected",
    )
    op.create_index(  # the job that opens deferred proposals again
        "proposals_deferred_idx", "proposals", ["deferred_until"], postgresql_where=col("state") == "deferred"
    )
    op.create_index(  # the Inbox items of a day
        "proposals_inbox_idx", "proposals", ["project_id", "inbox_at"], postgresql_where=col("inbox_at").is_not(None)
    )

    # notifications of proposals
    op.add_column("notifications", sa.Column("proposal_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "notifications_proposal_id_fkey", "notifications", "proposals", ["proposal_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_constraint("notifications_kind_check", "notifications", type_="check")
    op.create_check_constraint("notifications_kind_check", "notifications", col("kind").in_(NOTIFICATION_KINDS))
    op.create_check_constraint(
        "notifications_proposal_check", "notifications", (col("kind") == "proposal") == col("proposal_id").is_not(None)
    )
    op.create_index(
        "notifications_proposal_key",
        "notifications",
        ["proposal_id", "user_id"],
        unique=True,
        postgresql_where=col("proposal_id").is_not(None),
    )


def downgrade() -> None:
    runs = sa.table("runs", col("kind"), col("attempt"))
    notifications = sa.table("notifications", col("kind"))
    op.drop_index("notifications_proposal_key", table_name="notifications")
    op.drop_constraint("notifications_proposal_check", "notifications", type_="check")
    op.drop_constraint("notifications_kind_check", "notifications", type_="check")
    op.execute(notifications.delete().where(notifications.c.kind == "proposal"))
    op.create_check_constraint("notifications_kind_check", "notifications", col("kind").in_(NOTIFICATION_KINDS_0013))
    op.drop_constraint("notifications_proposal_id_fkey", "notifications", type_="foreignkey")
    op.drop_column("notifications", "proposal_id")
    for name in ("proposals_inbox_idx", "proposals_deferred_idx", "proposals_rejected_idx", "proposals_project_idx"):
        op.drop_index(name, table_name="proposals")
    op.drop_index("proposals_run_idx", table_name="proposals")
    op.drop_table("proposals")
    op.drop_index("findings_project_idx", table_name="findings")
    op.drop_index("findings_run_idx", table_name="findings")
    op.drop_table("findings")
    op.drop_table("curator_figures")
    op.drop_constraint("workers_run_kinds_check", "workers", type_="check")
    op.drop_column("workers", "run_kinds")
    op.drop_index("runs_active_review_key", table_name="runs")
    for name in ("runs_review_schedule_check", "runs_plan_revision_check", "runs_kind_plan_check"):
        op.drop_constraint(name, "runs", type_="check")
    for attempt in (3, 2, 1):  # the retries of a review run name the attempt before them
        op.execute(runs.delete().where(runs.c.kind == "review", runs.c.attempt == attempt))
    _drop_runs_checks()
    op.alter_column("runs", "plan_revision", existing_type=sa.Integer(), nullable=False)
    op.alter_column("runs", "plan_id", existing_type=sa.Text(), nullable=False)
    _runs_checks(KINDS_0013, "plan")
