"""The keys of the run the hub answers, as ``evo-agents hub run`` commands print it with --json: the contract of the
schema Run (``evo-agents hub contract print``), which ``evo_agents.hub.run_cli`` and the commands around it name. A key
the hub adds to Run is added here, or the contract test fails. Standard library only, like the rest of the client."""

from __future__ import annotations

RUN_KEYS = (
    "id",
    "kind",
    "project",
    "plan_id",
    "step_key",
    "title",
    "plan_revision",
    "dispatched_by",
    "dispatched_via",
    "worker_id",
    "worker",
    "pinned_worker_id",
    "requested_runtime",
    "runtime",
    "model",
    "mode",
    "approval",
    "timeout_min",
    "run_seconds",
    "attempt",
    "max_attempts",
    "parent_run_id",
    "resume_of_run_id",
    "state",
    "lease_expires_at",
    "session_id",
    "repo",
    "branch",
    "repos",
    "commit_sha",
    "diffstat",
    "verify",
    "evidence",
    "usage",
    "budget",
    "error",
    "log_sha256",
    "diff_sha256",
    "last_seq",
    "cancel_requested_at",
    "takeover_requested_at",
    "handback_requested_at",
    "queued_at",
    "leased_at",
    "started_at",
    "waiting_since",
    "parked_at",
    "finished_at",
    "request",
    "finish_requested_at",
    "steady_wait",
    "overlap_wait",
    "failure_cause",
    "session_per_step",
    "browser",
    "browser_identities",
)
RUN_KEYS += ("profile", "profile_id", "profile_version", "environment")  # its agent profile, where its agent runs
RUN_KEYS += ("machine", "cwd", "claude_email", "claude_profile", "last_activity_at")  # a local run's; null otherwise
RUN_KEYS += ("chat_id", "chat_message_id", "hop")  # a chat run's (evo_agents.hub.chat_runs); null for any other
RUN_KEYS += ("session_run_id", "session_turn", "session_turns", "session_title")  # a chat run's session; null otherwise
RUN_KEYS += ("principal", "autonomy")  # whom it acts for, and the autonomy snapshot (docs/autonomy.md)
RUN_KEYS += ("selector", "tolerations")  # where it goes: the labels it asks of its worker, the taints it tolerates
RUN_KEYS += ("plan_progress",)  # a plan run's steps done of its plan; null for any other kind
RUN_KEYS += ("as_owner", "as_owner_since")  # its owner lets it call the hub as them, since then (server/run_access.py)
RUN_KEYS += ("model_used", "routing")  # the model its runtime used, and what the model routing did (null: none)
