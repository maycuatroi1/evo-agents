"""Runs on the hub: the steps of a plan that may be dispatched, dispatching them, listing and showing runs, the
worker's side of the queue (claim, heartbeat, state) and the owner's cancel, approve, rerun, takeover and handback.
``docs/workers.md`` is the protocol, ``run_state`` moves the runs and records each move in the plan, and
``run_events`` holds a run's log, stream, messages and blobs.

The package has three layers, which lint-imports keeps in this order (contract hub-runs-layers in pyproject.toml).
``routes`` has one APIRouter a section: reading, listing, stats, dispatching, the owner's controls, the worker's side
and a plan run's plan. Each handler passes the request's parts to a public function of ``service``, which checks,
queries and moves runs with SQLAlchemy Core on ``evo_agents.hub.tables``. Both use ``models``, the request and
response models and the limits. The modules around the run API import from ``service`` and ``models``, never from
``routes``.

GET /v1/projects/{p}/plans/{plan}/ready-steps (reader) lists every step of the plan, ready or not, with why it is not
(``runs.unready_reason``, or the active run it has). POST /v1/projects/{p}/runs (writer) queues one run per step
named: every step must be ready and without an active run (409 otherwise, and nothing is queued), its repo one the
project lists an origin for (409 naming it: no credential is leased for a repo without one, and the worker's preflight
would fail the run; a rerun and a plan run are refused the same way), and a worker named must be one of the caller's
own (403 for any other id, a hub admin's included), live and serving the project (409).
Each run is notified on RUNS_CHANNEL and audited (run.dispatch), and keeps the step's title. ``model`` is optional,
one line of at most ``runs.MAX_MODEL_CHARS``, as the runtime names it; without one the runtime chooses as it would.
Each run keeps the credential it was dispatched with in dispatched_via (``credentials.dispatch_credential``): web for a
web session, machine for a token, schedule for a run the night shift of the project's charter queued
(``evo_agents.hub.server.curator``), which also has a budget: its caps, which its claim hands the worker with what it
spent already (``ClaimedBudget``). A worker whose owner set its dispatch_from to web takes runs dispatched from a web
session only: a dispatch, plan run or rerun pinned to it with a token gets 403 saying so, and its claims pass over
the runs dispatched with a token.

A plan the Curator made (``evo_agents.hub.server.changes``) is run by the night shift alone: a dispatch of its steps, a
plan run of it and a rerun of a run of it get 409, as does a rerun of a review run or a judge run, which only the night
shift queues. A judge run is claimed only by a worker whose daemon says it runs judge runs, with a checkout of its repo,
and comes with ``judge.build_judge_prompt``; the claim of any run of the Curator says so (``curator``: its role, the
charter's protected paths, and the change).

POST /v1/projects/{p}/plan-runs (writer) queues a plan run (``runs.RUN_KINDS``): one run, on one worker of the
caller's, that does every step of the plan not done yet. It is refused with 409 when the plan has no pending step,
when the plan has an active run of any kind, or when a step not done names no repo and the plan does not list exactly
one; the run's repos are those of the steps not done, each with the branch the plan's repos name. A dispatch of steps
and a dispatch of a plan run take the same transaction-scoped advisory lock of the plan (``plan_lock_key``) before
they look at its active runs, so the two exclude each other: a step of a plan with an active plan run gets 409 ("plan X
has plan run #N"), and a plan run waits until no run of the plan's steps is active. Each plan run is audited
(run.dispatch_plan). ready-steps names the plan's active plan run (``plan_run``), and no step is ready while it is.

POST /v1/projects/{p}/author-runs (writer) queues an author run (``evo_agents.hub.author``): a plan written from the
caller's request (at most ``author.MAX_REQUEST_BYTES`` of UTF-8, 422 otherwise) with create-exec-plan, on the worker of
the caller's it names (403 for any other id), on claude-code alone (422 for another runtime, saying why). Its repos are
the project's harness (409 for a project registered without one), then each repo of the project that worker has a
checkout of. A dispatch to a worker whose last heartbeat says no checkout of the harness, or a daemon that does not
run author runs, gets 409. It is audited (run.dispatch_author) without its request. A claim takes an author run only
with a checkout of the harness and a daemon that says it runs author runs, and hands it a presigned GET of the latest
version of the global skill create-exec-plan (``RunSpec.skills``); without that skill, or a blob store, the run fails
at the claim, saying so. The author runs of a new plan are listed and read as a review run is, through the project's
default label; one is never rerun (409). With ``plan_id`` it revises that plan, one the caller can read (404
otherwise), recorded with its revision. Its agent puts the plan with PUT /v1/worker/runs/{id}/plan
(``put_run_plan``), as the dispatcher, through ``plans.write_plan``; the run then points to the plan it wrote. Its
chat, waiting, parking and end are ``author_chat``'s: it reports ``waiting`` after each turn of its agent without a
decision of its own (409 once its owner ended the chat), which sends its owner the notice ``author_waiting``, and the
heartbeat says ``finish`` once the owner ended the chat.

GET /v1/projects/{p}/runs (reader) lists the project's runs newest first, filtered by state, plan, step, worker,
dispatcher and text, a page at a time, with how many runs each state has under the other filters; GET .../runs/{id}
shows one. A reader sees the runs of the plans it may read through the sink it names (``visible_plans``), and a run
of another plan reads as no run (``readable_run``). GET .../runs/stats counts the runs of the same plans that ended on
each of the last ``days`` UTC days (MIN_STATS_DAYS to MAX_STATS_DAYS, STATS_DAYS by default) in each end state, with
the 50th and 90th percentile of how long they ran (started, else leased, to finished, as the runs pages count it) and
the tokens of their usage, read as the run page's usage card reads it (``_usage_parts``); a day without a run has
zeros.

A worker claims with POST /v1/worker/claim, which waits up to CLAIM_WAIT_SECONDS: ``RunWakeups`` wakes it when
RUNS_CHANNEL is notified, on the api process's one LISTEN connection (``listen``, opened by the first claim or stream),
and it looks again every CLAIM_POLL_SECONDS besides, so a lost notification delays a claim but never loses it. A claim
takes the oldest queued run, ``FOR UPDATE SKIP LOCKED``, that its owner dispatched, of a project the worker serves and
on which the owner still holds writer, asking for a runtime the worker reported (``any`` takes the first of
runs.RUNTIMES it has), of a repo it has a checkout of, pinned to no other worker, dispatched from a web session when the
worker's dispatch_from is web, while the worker is neither draining nor revoked and holds fewer runs than its slots. The
next attempt of a run lost on the worker is passed over until the worker is steady (``steady``: heartbeats for
runs.STEADY_SECONDS, none runs.STEADY_GAP_SECONDS late), so a machine that slept does not take the run again at once;
any other worker that may take it takes it meanwhile, and Run's ``steady_wait`` names the worker it waits for. A worker
has one claim waiting at a time: a newer claim answers the older one with no run. The claimed run is leased for
EVO_HUB_RUN_LEASE_SECONDS (LEASE_SECONDS by default) and comes with its prompt (``runs.build_prompt`` over the plan
revision it was dispatched from). A plan run needs a checkout of every repo in its repos and a daemon of
runs.PLAN_RUN_AGENT or later, and comes with ``runs.build_plan_prompt`` and the plan at the hub's current revision,
which the daemon writes to ``runs.PLAN_FILE``. A dispatch pinned to a worker whose last heartbeat says it could never
claim the run gets 409 (``_fits``). A claim whose worker hung up (a daemon stopping drops the claim it waits on) takes
nothing: it ends before it looks at the queue again, and a run it leased in the meantime is rolled back before the
transaction commits, so the run stays queued for the next claim instead of waiting out a lease nobody holds.

POST /v1/worker/heartbeat records the machine (runtimes, each with the models it lists when it lists any, checkouts
keyed ``<project>/<repo>``, free slots) and when its heartbeats became steady (workers.steady_since, set again by a
heartbeat that comes runs.STEADY_GAP_SECONDS late), extends the lease of every run the worker names and still holds by
the same time, settles their agent time (``run_state.settle``), and answers with control: per run, whether to cancel
(asked by the owner, or a run the worker no longer holds), takeover, handback, terminal_open (a browser waits for the
worker's end of the run's terminal, ``terminal.Terminals.waiting``), how many inbox messages wait and how many decisions
of the run are open; for the worker, whether to drain. A run the reaper parked, or one done because a new run resumes
it, comes back with held false, cancel false and park true: the worker stops its agent at the end of the turn, keeps the
session and the worktrees, and frees the slot.

POST /v1/worker/runs/{id}/state reports a move of a run the worker holds (404 otherwise), checked against
``runs.TRANSITIONS`` with the worker as actor (409 otherwise): ``done`` only for approval auto with every verify
command exited 0, ``review`` only for approval review. A plan run ends ``done`` without verify results, since each of
its steps was verified when it was reported, and never in review. Reporting the state the run is in already changes
nothing but the session id, commit, diffstat, verify results and usage given, so a resend is safe. A ``failed``
report keeps its ``failure_cause`` (``runs.FAILURE_CAUSES``, an optional field of protocol version 1) as the run's,
which Run shows; a run of one step claimed carries its step's ``verify``, whose programs the worker's preflight looks
for before the agent starts. A plan run reports
``waiting`` when its agent's turn ended with a decision open, which needs a decision of the run that is open or whose
answer the worker has not taken yet (409 otherwise: waiting does not count toward the timeout), and ``running`` once
the answer reached the agent. A move out of the held states gives back the run's leases (``credentials``), and their
GitHub tokens are revoked once the move commits, before the answer.

The worker holding a plan run reads the plan as the hub holds it now with GET /v1/worker/runs/{id}/plan, and reports
each step with POST /v1/worker/runs/{id}/steps/{key}: ``in_progress``, ``done`` (with at least one verify result,
every one exited 0; 422 otherwise) or ``pending``, with evidence, the repo, the verify results and the commit. The hub
writes the step as the member who dispatched the run (``run_state.write_step``), answers with the error the plan's
write gets when it cannot, never sets a done step back (409), keeps the report as a ``system`` event of the run, and
audits it (run.step_report). A step the plan does not have, a run of one step, or a run the worker does not hold gets
404. A plan run that ends ``done`` writes no step; one that fails or is cancelled sets the steps it left
``in_progress`` back to ``pending`` (``run_state.release_plan_steps``).

The owner of a run is the member who dispatched it. Cancel moves a queued run or one in review to ``cancelled`` and
asks the worker holding a held run to stop it (the next heartbeat says cancel); approve moves a run in review to
``done``; rerun queues the step again, at the plan's current revision, with the run's model, after a run that ended.
Takeover asks the worker holding a leased or running run to let a person drive the agent in a terminal, and handback
asks it to let an interactive run's agent go on headless: the next heartbeat says takeover or handback until the
worker reports interactive or running, and any other move drops the ask. Each one is audited (run.cancel,
run.approve, run.rerun, run.takeover, run.handback; an ask repeated is not); another member gets 403, someone without
a grant on the project 404.
"""

from evo_agents.hub.server.runs.routes import router, worker_router
from evo_agents.hub.server.runs.service.claims import RunWakeups

__all__ = ["RunWakeups", "router", "worker_router"]
