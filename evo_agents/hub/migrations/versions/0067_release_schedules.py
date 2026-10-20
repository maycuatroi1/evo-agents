"""The daily release of a project: its schedule, its cycles, the runs and the tags of each cycle and every check.

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-21

``evo_agents.hub.releases`` describes the design and ``evo_agents.hub.server.releases`` the routes; the values below
are copies of their constants and of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions, as
``evo_agents.hub.tables_release`` describes the tables.

release_schedules holds the release schedule of a project, at most one: the repo, the local time HH:MM and the time
zone of its daily check, the target ref, the pattern of its tags (a literal prefix then *), the workflow that publishes
a tag, the packaging paths (a JSON array of 1 to 50 texts), the cap of one cycle in USD, the admin who set it (its
owner, whom the runs are dispatched as) and who wrote it last, and whether and by whom it is paused.

release_cycles holds each cycle a check opened: its state (open, stopped, published, closed; at most one open or
stopped per schedule), the mode of its first run, the owner it runs as, the local day of its check, the newest tag and
the commit of the target ref it started from, the packaging paths that changed, its cap, why it stopped or closed, and
when it ended (exactly when it is published or closed).

release_cycles also keeps the range its check read (span: the commits from the newest tag to the target, the pull
requests they name and the migrations the target adds, a JSON object, ``evo_agents.hub.release_runs.span_of``), why
it stopped last (stop_cause, ``evo_agents.hub.release_ci.STOP_CAUSES``, which every stop of the hub sets), and the decision
and the thread of the project's triage chat that stop opened.

release_runs holds the runs of each cycle with their mode (cut or fix) and, for a fix, the tag it fixes forward; what
the run pushed (the version, its branch release/<version>, the commit and the agent's Decision line); and how the hub
landed it: its pull request, the merge commit, the tag it made and when, or why it refused (land_state tagged,
refused or failed, land_reason, land_tries).
release_tags holds each tag a cycle follows (one row a tag of the cycle): its commit, the run whose merge commit the
hub tagged (none for a tag a person made), when the hub began to follow it, and the CI of the schedule's workflow
there: its state (pending, running, passed, failed, timeout), the workflow run, its attempt and start, how often the
hub ran its failed jobs again, the failures it read (a JSON object) and what kept it from reading.
release_checks holds every check: scheduled or manual (by an admin, requested_by), its local day, its outcome and
reason, what it read and the cycle and run it opened; the scheduled check of a day is kept once, a failed one aside.

runs.kind takes ``release``: a run on no plan and no single repo (its repos a JSON array), dispatched only by the
release schedule (dispatched_via ``release``, ``runs_release_check``), one active at a time per project
(``runs_active_release_key``). decisions.category takes ``release``, what the hub asks the owner of a cycle that
stopped; notifications.notice_kind takes ``release``, a cycle published or its CI cannot be read.

Going back deletes the release runs (and so their decisions), the release notices, drops the five tables and the index,
and puts back the checks of 0066, which is the schema that schema 0066 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

KINDS_0066 = ("step", "plan", "review", "judge", "author", "chat", "local", "task", "replay", "history")
KINDS = (*KINDS_0066, "release")  # evo_agents.hub.runs.SHOWN_KINDS
NO_PLAN_0066 = ("review", "chat", "task", "history")
NO_PLAN = (*NO_PLAN_0066, "release")
VIA_0066 = ("machine", "web", "schedule")
VIA = (*VIA_0066, "release")  # evo_agents.hub.credentials.DISPATCHED_VIA
ACTIVE = ("queued", "leased", "running", "interactive", "verifying", "waiting", "review", "parked")
CYCLE_STATES = ("open", "stopped", "published", "closed")  # evo_agents.hub.releases.CYCLE_STATES
UNFINISHED = ("open", "stopped")  # evo_agents.hub.releases.UNFINISHED
ENDED = ("published", "closed")
RUN_MODES = ("cut", "fix")  # evo_agents.hub.releases.RUN_MODES
TRIGGERS = ("schedule", "manual")  # evo_agents.hub.releases.CHECK_TRIGGERS
OUTCOMES = ("opened", "unchanged", "busy", "paused", "owner", "error")  # evo_agents.hub.releases.CHECK_OUTCOMES
AT = "^([01][0-9]|2[0-3]):[0-5][0-9]$"  # evo_agents.hub.releases.AT
TIME_ZONE = "^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$"  # evo_agents.hub.releases.TIME_ZONE
TAG_PATTERN = r"^[A-Za-z0-9._/-]{0,50}\*$"  # evo_agents.hub.releases.TAG_PATTERN
REF = "^[A-Za-z0-9._/-]{1,200}$"  # evo_agents.hub.releases.REF
WORKFLOW = r"^[A-Za-z0-9._-]{1,100}\.ya?ml$"  # evo_agents.hub.releases.WORKFLOW
OBJECT_NAME = "^([0-9a-f]{40}|[0-9a-f]{64})$"  # evo_agents.hub.releases.OBJECT_NAME
CYCLE_USD = (1.0, 10_000.0)  # evo_agents.hub.releases.CYCLE_USD
MAX_PATHS = 50  # evo_agents.hub.releases.MAX_PATHS
MAX_REASON_CHARS = 2000  # evo_agents.hub.releases.MAX_REASON_CHARS
MAX_TAG_CHARS = 200
CONTROL = r"[\x01-\x1f\x7f]"
NOT_A_TEXT = '$[*] ? (@.type() != "string")'  # an item of a JSON array that is not a text
RUN_CHECKS = ("runs_kind_check", "runs_kind_plan_check", "runs_dispatched_via_check")
VERSION = r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$"  # evo_agents.hub.releases.VERSION
BRANCH_PREFIX = "release/"  # evo_agents.hub.release_runs.BRANCH_PREFIX
LAND_STATES = ("tagged", "refused", "failed")  # evo_agents.hub.release_runs.LAND_STATES
MAX_URL_CHARS = 500  # evo_agents.hub.release_runs.MAX_URL_CHARS
CI_STATES = ("pending", "running", "passed", "failed", "timeout")  # evo_agents.hub.release_ci.CI_STATES
STOP_CAUSES = (  # evo_agents.hub.release_ci.STOP_CAUSES
    "run_failed",
    "run_cancelled",
    "refused",
    "github",
    "hacking",
    "budget",
    "same_test",
    "timeout",
    "ci",
)
CATEGORIES_0066 = (  # decisions_category_check as 0045 set it
    "deploy",
    "delete_data",
    "live_migration",
    "external_send",
    "spend_money",
    "architecture",
    "scope",
    "question",
    "triage",
)
CATEGORIES = (*CATEGORIES_0066, "release")  # evo_agents.hub.runs.SHOWN_DECISION_CATEGORIES
NOTICE_KINDS_0066 = (  # notifications_notice_kind_check as 0043 set it
    "push_default_branch",
    "merge_default_branch",
    "plan_finished",
    "run_failed",
    "curator_brief",
    "curator_paused",
    "author_waiting",
    "chat_mention",
    "chat_added",
    "plugin_disabled",
    "builder_proposal",
)
NOTICE_KINDS = (*NOTICE_KINDS_0066, "release")  # evo_agents.hub.runs.NOTICE_KINDS

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def _text(name: str, limit: int):
    """``name`` is NULL, or a text of 1 to ``limit`` characters without a control character."""
    value = col(name)
    return sa.or_(value.is_(None), sa.and_(sa.func.char_length(value).between(1, limit), ~value.op("~")(CONTROL)))


def _commit(name: str):
    return sa.or_(col(name).is_(None), col(name).regexp_match(OBJECT_NAME))


def _paths(name: str, *, least: int, nullable: bool):
    """``name`` is a JSON array of ``least`` to MAX_PATHS texts, or NULL when it may be."""
    value = col(name)
    texts_only = sa.cast(sa.literal(NOT_A_TEXT), postgresql.JSONPATH)
    shape = sa.case(
        (
            sa.func.jsonb_typeof(value) == "array",
            sa.and_(
                sa.func.jsonb_array_length(value).between(least, MAX_PATHS),
                ~sa.func.jsonb_path_exists(value, texts_only),
            ),
        ),
        else_=False,
    )
    return sa.or_(value.is_(None), shape) if nullable else shape


def _replace(name: str, rule) -> None:
    op.drop_constraint(name, "runs", type_="check")
    op.create_check_constraint(name, "runs", rule)


def _run_checks(kinds, no_plan, via) -> None:
    kind = col("kind")
    _replace("runs_kind_check", kind.in_(kinds))
    _replace(
        "runs_kind_plan_check", sa.or_(kind.in_(("author", "local")), kind.in_(no_plan) == col("plan_id").is_(None))
    )
    _replace("runs_dispatched_via_check", col("dispatched_via").in_(via))


def _create_schedules() -> None:
    op.create_table(
        "release_schedules",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("check_at", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("target_ref", sa.Text(), nullable=False),
        sa.Column("tag_pattern", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("paths", postgresql.JSONB(), nullable=False),
        sa.Column("cycle_budget_usd", sa.Double(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        _when("paused_at"),
        sa.Column("paused_by", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], ondelete="RESTRICT", name="release_schedules_owner_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["paused_by"], ["users.id"], ondelete="RESTRICT", name="release_schedules_paused_by_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="release_schedules_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], ondelete="RESTRICT", name="release_schedules_updated_by_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="release_schedules_pkey"),
        sa.UniqueConstraint("project_id", name="release_schedules_project_id_key"),
        sa.CheckConstraint(
            sa.and_(
                _text("repo", 200),
                col("check_at").regexp_match(AT),
                sa.func.char_length(col("timezone")) <= 64,
                col("timezone").regexp_match(TIME_ZONE),
                col("target_ref").regexp_match(REF),
                col("tag_pattern").regexp_match(TAG_PATTERN),
                col("workflow").regexp_match(WORKFLOW),
                _paths("paths", least=1, nullable=False),
                col("cycle_budget_usd").between(*CYCLE_USD),
                (col("paused_at").is_(None)) == (col("paused_by").is_(None)),
                col("updated_at") >= col("created_at"),
            ),
            name="release_schedules_check",
        ),
    )


def _create_cycles() -> None:
    op.create_table(
        "release_cycles",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("schedule_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), server_default="open", nullable=False),
        sa.Column("start", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("local_day", sa.Date(), nullable=False),
        sa.Column("base_tag", sa.Text(), nullable=True),
        sa.Column("base_commit", sa.Text(), nullable=True),
        sa.Column("target_ref", sa.Text(), nullable=False),
        sa.Column("target_commit", sa.Text(), nullable=True),
        sa.Column("changed_paths", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("budget_usd", sa.Double(), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("span", postgresql.JSONB(), nullable=True),
        _stamp("opened_at"),
        _stamp("updated_at"),
        _when("ended_at"),
        sa.Column("stop_cause", sa.Text(), nullable=True),
        sa.Column("decision_id", sa.BigInteger(), nullable=True),
        sa.Column("triage_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("triage_root_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["decisions.id"], ondelete="SET NULL", name="release_cycles_decision_id_fkey"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="release_cycles_owner_id_fkey"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="release_cycles_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["release_schedules.id"], ondelete="CASCADE", name="release_cycles_schedule_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["triage_chat_id"], ["chats.id"], ondelete="SET NULL", name="release_cycles_triage_chat_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["triage_root_id"], ["chat_messages.id"], ondelete="SET NULL", name="release_cycles_triage_root_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="release_cycles_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("state").in_(CYCLE_STATES),
                col("start").in_(RUN_MODES),
                _text("base_tag", MAX_TAG_CHARS),
                _commit("base_commit"),
                col("target_ref").regexp_match(REF),
                _commit("target_commit"),
                _paths("changed_paths", least=0, nullable=False),
                col("budget_usd") > 0,
                _text("stop_reason", MAX_REASON_CHARS),
                sa.or_(col("span").is_(None), sa.func.jsonb_typeof(col("span")) == "object"),
                col("state").in_(ENDED) == col("ended_at").is_not(None),
                sa.or_(col("stop_cause").is_(None), col("stop_cause").in_(STOP_CAUSES)),
            ),
            name="release_cycles_check",
        ),
    )
    op.create_index(
        "release_cycles_unfinished_key",
        "release_cycles",
        ["schedule_id"],
        unique=True,
        postgresql_where=col("state").in_(UNFINISHED),
    )
    op.create_index("release_cycles_schedule_idx", "release_cycles", ["schedule_id", "id"])


def _run_rule():
    """What a row of release_runs holds: its mode and red tag; a version X.Y.Z with its branch release/<version> and
    the commit pushed, all three or none; the pull request, merge commit and tag the hub made; and how it landed."""
    version, tagged = col("version"), col("tagged")
    return sa.and_(
        col("mode").in_(RUN_MODES),
        _text("tag", MAX_TAG_CHARS),
        sa.or_(version.is_(None), version.regexp_match(VERSION)),
        version.is_(None) == col("branch").is_(None),
        version.is_(None) == col("head_commit").is_(None),
        sa.or_(col("branch").is_(None), col("branch") == sa.literal(BRANCH_PREFIX) + version),
        _commit("head_commit"),
        _text("decision", MAX_REASON_CHARS),
        sa.or_(col("pull_number").is_(None), col("pull_number") > 0),
        _text("pull_url", MAX_URL_CHARS),
        _commit("merge_commit"),
        _text("tagged", MAX_TAG_CHARS),
        tagged.is_(None) == col("tagged_at").is_(None),
        sa.or_(col("land_state").is_(None), col("land_state").in_(LAND_STATES)),
        sa.func.coalesce(col("land_state") == "tagged", False) == tagged.is_not(None),
        _text("land_reason", MAX_REASON_CHARS),
        col("land_tries") >= 0,
    )


def _create_runs() -> None:
    op.create_table(
        "release_runs",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("tag", sa.Text(), nullable=True),
        _stamp("created_at"),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("head_commit", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("pull_number", sa.BigInteger(), nullable=True),
        sa.Column("pull_url", sa.Text(), nullable=True),
        sa.Column("merge_commit", sa.Text(), nullable=True),
        sa.Column("tagged", sa.Text(), nullable=True),
        _when("tagged_at"),
        sa.Column("land_state", sa.Text(), nullable=True),
        sa.Column("land_reason", sa.Text(), nullable=True),
        sa.Column("land_tries", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["cycle_id"], ["release_cycles.id"], ondelete="CASCADE", name="release_runs_cycle_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="release_runs_run_id_fkey"),
        sa.PrimaryKeyConstraint("run_id", name="release_runs_pkey"),
        sa.CheckConstraint(_run_rule(), name="release_runs_check"),
    )
    op.create_index("release_runs_cycle_idx", "release_runs", ["cycle_id", "run_id"])


def _tag_rule():
    """What a row of release_tags holds: a tag and its commit, the state of its CI, the workflow run and its attempt,
    the hub's runs again, the failures as a JSON object and the problem it last met."""
    return sa.and_(
        _text("tag", MAX_TAG_CHARS),
        col("commit").regexp_match(OBJECT_NAME),
        col("ci_state").in_(CI_STATES),
        sa.or_(col("workflow_run_id").is_(None), col("workflow_run_id") > 0),
        _text("workflow_url", MAX_URL_CHARS),
        sa.or_(col("run_attempt").is_(None), col("run_attempt") > 0),
        col("reruns") >= 0,
        sa.or_(col("rerun_of_attempt").is_(None), col("rerun_of_attempt") > 0),
        sa.or_(col("failures").is_(None), sa.func.jsonb_typeof(col("failures")) == "object"),
        _text("problem", MAX_REASON_CHARS),
    )


def _create_tags() -> None:
    op.create_table(
        "release_tags",
        _id(),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        sa.Column("commit", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _stamp("watched_since"),
        sa.Column("ci_state", sa.Text(), server_default="pending", nullable=False),
        sa.Column("workflow_run_id", sa.BigInteger(), nullable=True),
        sa.Column("workflow_url", sa.Text(), nullable=True),
        sa.Column("run_attempt", sa.Integer(), nullable=True),
        sa.Column("reruns", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rerun_of_attempt", sa.Integer(), nullable=True),
        _when("started_at"),
        _when("checked_at"),
        sa.Column("failures", postgresql.JSONB(), nullable=True),
        sa.Column("problem", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["cycle_id"], ["release_cycles.id"], ondelete="CASCADE", name="release_tags_cycle_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="release_tags_run_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="release_tags_pkey"),
        sa.UniqueConstraint("cycle_id", "tag", name="release_tags_cycle_id_tag_key"),
        sa.CheckConstraint(_tag_rule(), name="release_tags_check"),
    )


def _replace_in(table: str, name: str, column: str, values) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, col(column).in_(values))


def _create_checks() -> None:
    op.create_table(
        "release_checks",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("schedule_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("local_day", sa.Date(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("base_tag", sa.Text(), nullable=True),
        sa.Column("base_commit", sa.Text(), nullable=True),
        sa.Column("target_commit", sa.Text(), nullable=True),
        sa.Column("changed_paths", postgresql.JSONB(), nullable=True),
        sa.Column("cycle_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _stamp("checked_at"),
        sa.ForeignKeyConstraint(
            ["cycle_id"], ["release_cycles.id"], ondelete="SET NULL", name="release_checks_cycle_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="release_checks_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], ondelete="RESTRICT", name="release_checks_requested_by_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="release_checks_run_id_fkey"),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["release_schedules.id"], ondelete="CASCADE", name="release_checks_schedule_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="release_checks_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("trigger").in_(TRIGGERS),
                (col("trigger") == "manual") == col("requested_by").is_not(None),
                col("outcome").in_(OUTCOMES),
                _text("reason", MAX_REASON_CHARS),
                _text("base_tag", MAX_TAG_CHARS),
                _commit("base_commit"),
                _commit("target_commit"),
                _paths("changed_paths", least=0, nullable=True),
            ),
            name="release_checks_check",
        ),
    )
    op.create_index(
        "release_checks_day_key",
        "release_checks",
        ["schedule_id", "local_day"],
        unique=True,
        postgresql_where=sa.and_(col("trigger") == "schedule", col("outcome") != "error"),
    )
    op.create_index("release_checks_schedule_idx", "release_checks", ["schedule_id", "id"])


def upgrade() -> None:
    _run_checks(KINDS, NO_PLAN, VIA)
    op.create_check_constraint(
        "runs_release_check", "runs", (col("kind") == "release") == (col("dispatched_via") == "release")
    )
    op.create_index(
        "runs_active_release_key",
        "runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.and_(col("kind") == "release", col("state").in_(ACTIVE)),
    )
    _replace_in("decisions", "decisions_category_check", "category", CATEGORIES)
    _replace_in("notifications", "notifications_notice_kind_check", "notice_kind", NOTICE_KINDS)
    _create_schedules()
    _create_cycles()
    _create_runs()
    _create_tags()
    _create_checks()


def downgrade() -> None:
    for name in ("release_checks", "release_tags", "release_runs", "release_cycles", "release_schedules"):
        op.drop_table(name)
    runs = sa.table("runs", col("kind"), col("attempt"))
    for attempt in (3, 2, 1):  # a retry names the attempt before it: delete the retries first
        op.execute(runs.delete().where(runs.c.kind == "release", runs.c.attempt == attempt))
    decisions = sa.table("decisions", col("category"))
    op.execute(decisions.delete().where(decisions.c.category == "release"))
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == "release"))
    _replace_in("notifications", "notifications_notice_kind_check", "notice_kind", NOTICE_KINDS_0066)
    _replace_in("decisions", "decisions_category_check", "category", CATEGORIES_0066)
    op.drop_index("runs_active_release_key", table_name="runs")
    op.drop_constraint("runs_release_check", "runs", type_="check")
    _run_checks(KINDS_0066, NO_PLAN_0066, VIA_0066)
