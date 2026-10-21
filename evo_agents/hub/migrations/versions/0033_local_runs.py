"""Local runs: a Claude Code session on a member's own machine, which the hub watches and asks decisions for.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-17

``evo_agents.hub.server.local_runs`` describes the design; the lists and bounds below are copies of
``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions.

runs.kind takes a seventh value, 'local', and runs.state two more, 'idle' and 'ended', which only a local run has. A
local run has no worker, lease, slot or attempts: it is neither queued nor held, so the checks that tie a state to a
worker, a lease or the timeout clock leave it out, and 'ended' (as the states 'done', 'failed', 'lost' and
'cancelled') sets finished_at. It takes a plan and a step when the session works on one (plan_id, plan_revision and
step_key, which a step run alone had), and no repos. runs gets machine, cwd, claude_email, claude_profile (all text)
and last_activity_at (the last time the session said anything): the first three, session_id and last_activity_at are
set for a local run, and every one of the five is NULL for any other kind. runs_local_session_key makes a session
register once per project; runs_local_silent_idx finds the sessions that went silent.

decisions.category takes 'question', which the agent of a local run asks when a question of the person fits no other
category (``evo_agents.hub.runs.LOCAL_DECISION_CATEGORY``).

Going back deletes the local runs (their events, inbox and decisions go with them) and puts the checks of 0032 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

KINDS_0032 = ("step", "plan", "review", "judge", "author", "chat")
KINDS = (*KINDS_0032, "local")  # evo_agents.hub.runs.SHOWN_KINDS
STATES_0032 = (
    "queued",
    "leased",
    "running",
    "interactive",
    "verifying",
    "waiting",
    "review",
    "parked",
    "done",
    "failed",
    "lost",
    "cancelled",
)
LOCAL_ONLY_STATES = ("idle", "ended")  # evo_agents.hub.runs.LOCAL_ONLY_STATES
STATES = (*STATES_0032, *LOCAL_ONLY_STATES)
LOCAL_STATES = ("running", "waiting", "idle", "ended")  # evo_agents.hub.runs.LOCAL_STATES
HELD = ("leased", "running", "interactive", "verifying", "waiting")
FINISHED_0032 = ("done", "failed", "lost", "cancelled")
FINISHED = (*FINISHED_0032, "ended")
MAX_FIELD = 500  # evo_agents.hub.runs.MAX_LOCAL_FIELD
CATEGORIES_0032 = (
    "deploy",
    "delete_data",
    "live_migration",
    "external_send",
    "spend_money",
    "architecture",
    "scope",
)
CATEGORIES = (*CATEGORIES_0032, "question")  # evo_agents.hub.runs.LOCAL_DECISION_CATEGORIES
LOCAL_COLUMNS = ("machine", "cwd", "claude_email", "claude_profile", "last_activity_at")
LOCAL_CHECKS = ("runs_local_check", "runs_local_state_check", "runs_local_field_check")

col = sa.column


def _replace(name: str, rule) -> None:
    op.drop_constraint(name, "runs", type_="check")
    op.create_check_constraint(name, "runs", rule)


def _checks(kinds, states, finished, *, local: bool) -> None:
    """The checks of runs that name its kinds or states: those of 0032 without ``local``, else with the local run."""
    kind, state = col("kind"), col("state")
    _replace("runs_kind_check", kind.in_(kinds))
    _replace("runs_state_check", state.in_(states))
    held = state.in_(("queued", "cancelled", "failed"))
    _replace(
        "runs_check5",
        sa.or_(
            held,
            kind == "local" if local else sa.false(),
            sa.and_(col("worker_id").is_not(None), col("runtime") != "any", col("leased_at").is_not(None)),
        ),
    )
    _replace("runs_check7", state.in_(finished) == col("finished_at").is_not(None))
    _replace(
        "runs_held_lease_check",
        sa.or_(
            state.not_in(HELD),
            col("lease_expires_at").is_not(None),
            kind == "local" if local else sa.false(),
        ),
    )
    on_no_plan = kind.in_(("review", "chat")) == col("plan_id").is_(None)
    _replace(
        "runs_kind_plan_check",
        sa.or_(kind.in_(("author", "local")), on_no_plan) if local else sa.or_(kind == "author", on_no_plan),
    )
    step_key = (kind == "step") == col("step_key").is_not(None)
    _replace("runs_kind_step_key_check", sa.or_(kind == "local", step_key) if local else step_key)
    repos = (kind == "step") == col("repos").is_(None)
    _replace(
        "runs_kind_repos_check",
        sa.or_(kind.in_(("chat", "local")), repos) if local else sa.or_(kind == "chat", repos),
    )


def _categories(categories) -> None:
    op.drop_constraint("decisions_category_check", "decisions", type_="check")
    op.create_check_constraint("decisions_category_check", "decisions", col("category").in_(categories))


def upgrade() -> None:
    _checks(KINDS, STATES, FINISHED, local=True)
    _categories(CATEGORIES)
    for name in ("machine", "cwd", "claude_email", "claude_profile"):
        op.add_column("runs", sa.Column(name, sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
    kind, state = col("kind"), col("state")
    required = ("session_id", "machine", "cwd", "claude_email", "last_activity_at")
    op.create_check_constraint(
        "runs_local_check",
        "runs",
        sa.or_(
            sa.and_(kind == "local", col("worker_id").is_(None), *(col(c).is_not(None) for c in required)),
            sa.and_(
                kind != "local",
                *(col(c).is_(None) for c in ("machine", "cwd", "claude_email", "claude_profile", "last_activity_at")),
            ),
        ),
    )
    op.create_check_constraint(
        "runs_local_state_check",
        "runs",
        sa.and_(
            sa.or_(kind == "local", state.not_in(LOCAL_ONLY_STATES)),
            sa.or_(kind != "local", state.in_(LOCAL_STATES)),
        ),
    )
    op.create_check_constraint(
        "runs_local_field_check",
        "runs",
        sa.and_(
            *(
                sa.or_(
                    col(c).is_(None),
                    sa.and_(
                        sa.func.char_length(col(c)).between(1, MAX_FIELD),
                        col(c).op("!~")(r"[\x01-\x1f\x7f]"),
                    ),
                )
                for c in ("machine", "cwd", "claude_email", "claude_profile")
            )
        ),
    )
    op.create_index(
        "runs_local_session_key",
        "runs",
        ["project_id", "session_id"],
        unique=True,
        postgresql_where=kind == "local",
    )
    op.create_index(
        "runs_local_silent_idx",
        "runs",
        ["last_activity_at"],
        postgresql_where=sa.and_(kind == "local", state != "ended"),
    )


def downgrade() -> None:
    runs = sa.table("runs", col("kind"))
    op.execute(runs.delete().where(runs.c.kind == "local"))
    op.drop_index("runs_local_silent_idx", table_name="runs")
    op.drop_index("runs_local_session_key", table_name="runs")
    for name in LOCAL_CHECKS:
        op.drop_constraint(name, "runs", type_="check")
    for name in reversed(LOCAL_COLUMNS):
        op.drop_column("runs", name)
    _checks(KINDS_0032, STATES_0032, FINISHED_0032, local=False)
    _categories(CATEGORIES_0032)
