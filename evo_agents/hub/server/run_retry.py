"""The row of the next attempt of a run the hub lost: what a retry copies from the run it goes on from."""

from __future__ import annotations

from sqlalchemy import insert, select

from evo_agents.hub import tables

CHAT_COLUMNS = ("chat_id", "chat_message_id", "hop")  # a chat run's retry keeps them; NULL for any other kind


def next_attempt(run_id: int):
    """The next attempt of run ``run_id``: the same step, ``parent_run_id`` pointing back, the attempt one higher and
    the runtime the dispatch asked for. It keeps the dispatch's credential, so a worker that takes runs dispatched
    from the web only takes the retry of one too, and never the retry of a run dispatched with a token; the schedule,
    night and caps of a run the night shift queued; the agent profile and version it went under; its plugin config; a
    task run's agent type and pull requests; its principal and autonomy snapshot; whether it gets a browser."""
    r = tables.runs
    copied = (
        "kind",
        "project_id",
        "plan_id",
        "step_key",
        "title",
        "plan_revision",
        "dispatched_by",
        "dispatched_via",
        "pinned_worker_id",
        "requested_runtime",
        "mode",
        "approval",
        "timeout_s",
        "max_attempts",
        "repo",
        "branch",
        "repos",
        "model",
        "schedule_id",
        "schedule_night",
        "budget",
        "request",
        "profile_id",
        "profile_version",
        "plugin_config",
        "agent_type_id",
        "pulls",
        "browser",
        "browser_identities",
        "principal_id",
        "autonomy",
        *CHAT_COLUMNS,
    )
    source = select(
        *(r.c[name] for name in copied),
        r.c.requested_runtime.label("runtime"),
        (r.c.attempt + 1).label("attempt"),
        r.c.id.label("parent_run_id"),
    ).where(r.c.id == run_id)
    return insert(r).from_select([*copied, "runtime", "attempt", "parent_run_id"], source).returning(r.c.id)
