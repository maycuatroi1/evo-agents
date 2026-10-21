"""Baselines of the core plugin canvas, the hub's events for the Loop, and three events plugins may take.

Revision ID: 0077
Revises: 0076
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_canvas``, ``evo_agents.hub.tables_signals``,
``evo_agents.hub.hub_events``, ``evo_agents.hub.plugin_events``, ``evo_agents.plugins.manifest`` and
``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions.

canvas_baselines holds the baselines of a canvas, numbered within it: a weekly one (its ISO week once per canvas, when
it was due and in which time zone) or one a writer opens at once for a claim (``push``); proposed, approved with a
snapshot, or expired. canvas_baseline_items holds one claim of a baseline: what it asks, the members who may not decide
it, and the decision. canvas_claim_revs.baseline_id names the baseline whose decision wrote a rev.

hub_events is the hub's log of the events of each project, only ever added to, which the Loop reads by cursor: a kind
of two to four dotted names, a payload object, and when.

plugin_deliveries.kind takes canvas.claim, canvas.baseline and signal.posted; project_plugins.permissions takes
canvas:read and signals:read; notifications.notice_kind takes canvas_baseline.

Going back deletes the deliveries of the three events and the canvas_baseline notices, takes the two permissions out
of every grant, puts the checks of 0076 back, and drops the column and the three tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None

STATUSES = ("assumption", "testing", "validated", "invalidated", "retired")
BASELINE_KINDS = ("weekly", "push")
BASELINE_STATUSES = ("proposed", "approved", "expired")
ITEM_KINDS = ("status", "value_check", "overdue", "new")
ASKED = ("status", "value_check")  # the kinds of item that ask for a status
WEEK = r"^[0-9]{4}-W[0-9]{2}$"
MAX_TIME_ZONE_CHARS = 64
MAX_REASON_CHARS = 1_000
EVENT_KIND = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$"
NEW_EVENTS = ("canvas.claim", "canvas.baseline", "signal.posted")
DELIVERY_KINDS_0042 = ("run.state", "chat.message", "plan.step", "notification", "task.result")
DELIVERY_KINDS = (*DELIVERY_KINDS_0042, *NEW_EVENTS)
DELIVERY_STATES = ("pending", "delivered", "failed")
NAME_PATTERN = "^[a-z][a-z0-9_-]{0,62}$"
MAX_ERROR_CHARS = 2000
PERMISSIONS_0035 = (
    "runs:read",
    "runs:dispatch",
    "chats:read",
    "chats:write",
    "plans:read",
    "memories:read",
    "notifications:send",
)
NEW_PERMISSIONS = ("canvas:read", "signals:read")
PERMISSIONS = (*PERMISSIONS_0035, *NEW_PERMISSIONS)
NOTICE_KINDS_0071 = (  # notifications_notice_kind_check as 0067 set it
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
    "release",
)
CANVAS_NOTICES = ("canvas_baseline",)
NOTICE_KINDS = (*NOTICE_KINDS_0071, *CANVAS_NOTICES)

col = sa.column
REV_KEY = "canvas_claim_revs_baseline_id_fkey"
REV_CHECK = "canvas_claim_revs_baseline_check"
INDEXES = (  # (index, table), dropped before the tables
    ("canvas_baseline_items_claim_idx", "canvas_baseline_items"),
    ("canvas_baselines_status_idx", "canvas_baselines"),
    ("hub_events_project_idx", "hub_events"),
)
TABLES = ("canvas_baseline_items", "canvas_baselines", "hub_events")  # in the order going back drops them


def _key(table: str, column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _object(name: str):
    return sa.func.jsonb_typeof(col(name)) == "object"


def _replace(table: str, name: str, rule) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, rule)


def _checks(delivery_kinds, permissions, notice_kinds) -> None:
    """The checks that name the kinds of deliveries, the permissions and the kinds of notice."""
    _replace(
        "plugin_deliveries",
        "plugin_deliveries_check",
        sa.and_(
            col("kind").in_(delivery_kinds),
            col("contribution").regexp_match(NAME_PATTERN),
            col("state").in_(DELIVERY_STATES),
            col("attempts") >= 0,
            sa.func.char_length(col("last_error")) <= MAX_ERROR_CHARS,
            (col("state") == "delivered") == col("delivered_at").is_not(None),
        ),
    )
    granted = col("permissions").op("<@")(postgresql.array(permissions, type_=sa.Text()))
    _replace("project_plugins", "project_plugins_permissions_check", granted)
    _replace("notifications", "notifications_notice_kind_check", col("notice_kind").in_(notice_kinds))


def _events() -> None:
    name = "hub_events"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _stamp("created_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="hub_events_pkey"),
        sa.CheckConstraint(sa.and_(col("kind").regexp_match(EVENT_KIND), _object("payload")), name="hub_events_check"),
    )
    op.create_index("hub_events_project_idx", name, ["project_id", "id"])


def _baseline_check():
    weekly = col("kind") == "weekly"
    approved = col("status") == "approved"
    return sa.and_(
        col("number") >= 1,
        col("kind").in_(BASELINE_KINDS),
        col("status").in_(BASELINE_STATUSES),
        # a weekly baseline names its week, when it was due and in which zone; a writer opens one at once
        weekly == col("week").is_not(None),
        weekly == col("due_at").is_not(None),
        weekly == col("time_zone").is_not(None),
        (col("kind") == "push") == col("created_by").is_not(None),
        sa.or_(col("week").is_(None), col("week").regexp_match(WEEK)),
        sa.or_(col("time_zone").is_(None), sa.func.char_length(col("time_zone")).between(1, MAX_TIME_ZONE_CHARS)),
        _object("label"),
        # approved holds when and its snapshot, a list of the claims; expired holds when
        approved == col("approved_at").is_not(None),
        approved == col("snapshot").is_not(None),
        sa.or_(col("snapshot").is_(None), sa.func.jsonb_typeof(col("snapshot")) == "array"),
        (col("status") == "expired") == col("expired_at").is_not(None),
    )


def _baselines() -> None:
    name = "canvas_baselines"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("canvas_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("week", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_zone", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _key(name, "canvas_id", "canvas_canvases", ondelete="CASCADE"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="canvas_baselines_pkey"),
        sa.UniqueConstraint("canvas_id", "number", name="canvas_baselines_number_key"),
        sa.UniqueConstraint("canvas_id", "week", name="canvas_baselines_week_key"),
        sa.CheckConstraint(_baseline_check(), name="canvas_baselines_check"),
    )
    op.create_index("canvas_baselines_status_idx", name, ["canvas_id", "status"])


def _item_check():
    kind, decision = col("kind"), col("decision")
    decided = decision.is_not(None)
    return sa.and_(
        kind.in_(ITEM_KINDS),
        col("from_status").in_(STATUSES),
        col("at_status").in_(STATUSES),
        col("claim_rev") >= 1,
        # what each kind asks: a status, nothing yet (an overdue card), or assumption as it is (a new claim)
        sa.or_(sa.not_(kind.in_(ASKED)), sa.and_(col("to_status").is_not(None), col("to_status").in_(STATUSES))),
        sa.or_(kind != "overdue", col("to_status").is_(None)),
        sa.or_(kind != "new", col("to_status").is_not_distinct_from("assumption")),
        sa.or_(kind == "value_check", col("proposal_id").is_(None)),
        (kind == "overdue") == col("deadline").is_not(None),
        _object("label"),
        # the decisions each kind takes; a new claim is included by the hub, every other decision is a member's
        sa.or_(
            decision.is_(None),
            sa.and_(kind.in_(ASKED), decision.in_(("accepted", "rejected", "deferred"))),
            sa.and_(kind == "overdue", decision.in_(("extended", "retired", "deferred"))),
            sa.and_(kind == "new", decision == "included"),
        ),
        decided == col("decided_at").is_not(None),
        sa.and_(decided, kind != "new") == col("decided_by").is_not(None),
        decision.is_not_distinct_from("extended") == col("new_deadline").is_not(None),
        sa.or_(col("reason").is_(None), sa.and_(decided, sa.func.char_length(col("reason")).between(1, 1000))),
        sa.or_(col("rev").is_(None), sa.and_(decided, col("rev") >= 1)),
    )


def _items() -> None:
    name = "canvas_baseline_items"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("baseline_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=False),
        sa.Column("at_status", sa.Text(), nullable=False),
        sa.Column("to_status", sa.Text(), nullable=True),
        sa.Column("claim_rev", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=True),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("evidence_ids", postgresql.ARRAY(sa.BigInteger()), server_default="{}", nullable=False),
        sa.Column("proposers", postgresql.ARRAY(sa.BigInteger()), server_default="{}", nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_deadline", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("rev", sa.Integer(), nullable=True),
        _key(name, "baseline_id", "canvas_baselines", ondelete="CASCADE"),
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "proposal_id", "canvas_proposals", ondelete="SET NULL"),
        _key(name, "decided_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="canvas_baseline_items_pkey"),
        sa.UniqueConstraint("baseline_id", "claim_id", name="canvas_baseline_items_claim_key"),
        sa.CheckConstraint(_item_check(), name="canvas_baseline_items_check"),
    )
    op.create_index("canvas_baseline_items_claim_idx", name, ["claim_id"])


def upgrade() -> None:
    _events()
    _baselines()
    _items()
    op.add_column("canvas_claim_revs", sa.Column("baseline_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        REV_KEY, "canvas_claim_revs", "canvas_baselines", ["baseline_id"], ["id"], ondelete="SET NULL"
    )
    # only the decision on an item of a baseline names one
    op.create_check_constraint(
        REV_CHECK, "canvas_claim_revs", sa.or_(col("baseline_id").is_(None), col("change") == "baseline")
    )
    _checks(DELIVERY_KINDS, PERMISSIONS, NOTICE_KINDS)


def downgrade() -> None:
    deliveries = sa.table("plugin_deliveries", col("kind"))
    op.execute(deliveries.delete().where(deliveries.c.kind.in_(NEW_EVENTS)))
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind.in_(CANVAS_NOTICES)))
    granted = sa.table("project_plugins", col("permissions"))
    kept = granted.c.permissions
    for permission in NEW_PERMISSIONS:
        kept = sa.func.array_remove(kept, permission)
    op.execute(granted.update().values(permissions=kept))
    _checks(DELIVERY_KINDS_0042, PERMISSIONS_0035, NOTICE_KINDS_0071)
    op.drop_constraint(REV_CHECK, "canvas_claim_revs", type_="check")
    op.drop_constraint(REV_KEY, "canvas_claim_revs", type_="foreignkey")
    op.drop_column("canvas_claim_revs", "baseline_id")
    for index, table in INDEXES:
        op.drop_index(index, table_name=table)
    for table in TABLES:
        op.drop_table(table)
