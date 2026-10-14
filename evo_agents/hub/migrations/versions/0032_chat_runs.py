"""Chat runs: one turn of an agent mentioned in a group chat, and the budget of a chat.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-14

``evo_agents.hub.chat_runs`` describes the design; the lists and bounds below are copies of its constants and of
``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables_chats`` describes the
columns.

runs.kind takes a sixth value, 'chat': a run on no plan (plan_id NULL, as a review run's), with repos or none (a
project that lists no repo), that may go on from an earlier run in its session (resume_of_run_id, as a plan or author
run). runs gets chat_id, the chat of a chat run, chat_message_id, the message that started it (the mention, or the
reply that woke its session), and hop, how many hands the work passed through from a person's message (1 to 3): all
three set for a chat run and NULL for any other. runs_chat_idx finds the chat runs of a chat by when they were queued,
for the chat's budget. chats gets budget_usd, what its chat runs may cost in 24 hours: more than 0, at most 1000,
5 by default.

Going back deletes the chat runs, drops the columns, the index and the checks, and puts the checks of 0031 back, which
is the schema that schema 0031 runs on.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

KINDS_0031 = ("step", "plan", "review", "judge", "author")
KINDS = (*KINDS_0031, "chat")  # evo_agents.hub.runs.RUN_KINDS
RESUME_0031 = ("plan", "author")
RESUME = (*RESUME_0031, "chat")  # evo_agents.hub.runs.SESSION_KINDS
MAX_HOPS = 3  # evo_agents.hub.chat_runs.MAX_HOPS
DEFAULT_BUDGET_USD = 5  # evo_agents.hub.chat_runs.DEFAULT_BUDGET_USD
MAX_BUDGET_USD = 1000  # evo_agents.hub.chat_runs.MAX_BUDGET_USD
CHAT_CHECKS = ("runs_chat_check", "runs_chat_message_check", "runs_hop_check")

col = sa.column


def _replace(name: str, rule) -> None:
    op.drop_constraint(name, "runs", type_="check")
    op.create_check_constraint(name, "runs", rule)


def _kind_checks(kinds, resume, *, chat: bool) -> None:
    """The checks of runs that name its kinds: those of 0031 without ``chat``, else with the chat run."""
    _replace("runs_kind_check", col("kind").in_(kinds))
    on_no_plan = col("kind").in_(("review", "chat")) if chat else col("kind") == "review"
    _replace("runs_kind_plan_check", sa.or_(col("kind") == "author", on_no_plan == col("plan_id").is_(None)))
    repos = (col("kind") == "step") == col("repos").is_(None)
    _replace("runs_kind_repos_check", sa.or_(col("kind") == "chat", repos) if chat else repos)
    resumes = sa.and_(col("kind").in_(resume), col("resume_of_run_id") != col("id"))
    _replace("runs_resume_check", sa.or_(col("resume_of_run_id").is_(None), resumes))


def upgrade() -> None:
    _kind_checks(KINDS, RESUME, chat=True)
    op.add_column("runs", sa.Column("chat_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("chat_message_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("hop", sa.SmallInteger(), nullable=True))
    op.create_foreign_key("runs_chat_id_fkey", "runs", "chats", ["chat_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(
        "runs_chat_message_id_fkey", "runs", "chat_messages", ["chat_message_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_check_constraint("runs_chat_check", "runs", (col("kind") == "chat") == col("chat_id").is_not(None))
    op.create_check_constraint(
        "runs_chat_message_check",
        "runs",
        sa.and_(
            col("chat_id").is_(None) == col("chat_message_id").is_(None),
            col("chat_id").is_(None) == col("hop").is_(None),
        ),
    )
    op.create_check_constraint("runs_hop_check", "runs", col("hop").between(1, MAX_HOPS))
    op.create_index("runs_chat_idx", "runs", ["chat_id", "queued_at"], postgresql_where=col("chat_id").is_not(None))
    op.add_column("chats", sa.Column("budget_usd", sa.Double(), server_default=str(DEFAULT_BUDGET_USD), nullable=False))
    op.create_check_constraint(
        "chats_budget_check", "chats", sa.and_(col("budget_usd") > 0, col("budget_usd") <= MAX_BUDGET_USD)
    )


def downgrade() -> None:
    runs = sa.table("runs", col("kind"), col("attempt"), col("resume_of_run_id"))
    op.drop_constraint("chats_budget_check", "chats", type_="check")
    op.drop_column("chats", "budget_usd")
    # each chat run goes on from the one before it, and a retry names the attempt before it: undo the first, then
    # delete the retries before the attempts they retry
    op.execute(runs.update().where(runs.c.kind == "chat").values(resume_of_run_id=None))
    for attempt in (3, 2, 1):
        op.execute(runs.delete().where(runs.c.kind == "chat", runs.c.attempt == attempt))
    op.drop_index("runs_chat_idx", table_name="runs")
    for name in CHAT_CHECKS:
        op.drop_constraint(name, "runs", type_="check")
    op.drop_constraint("runs_chat_message_id_fkey", "runs", type_="foreignkey")
    op.drop_constraint("runs_chat_id_fkey", "runs", type_="foreignkey")
    for name in ("hop", "chat_message_id", "chat_id"):
        op.drop_column("runs", name)
    _kind_checks(KINDS_0031, RESUME_0031, chat=False)
