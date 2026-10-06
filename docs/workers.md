# Workers and runs

A worker is a member's own laptop or desktop, registered with the hub, that runs plan steps for that member. The
member dispatches a ready step; the hub queues a run; the member's worker claims it, runs the step with Claude Code,
opencode or Codex CLI, headless or with a person at the keyboard, and sends logs, state and evidence back while it
works. The hub then records the step's progress in the plan. From 0.4.0 a member can also hand a whole plan to one of
their workers, as a plan run that does every step not done yet and asks the member only the decisions that matter
(see [Plan runs](#plan-runs)). The worker always calls the hub over HTTPS and never opens a port on the machine.

This page describes version 1 of the worker protocol. `evo_agents/hub/runs.py` holds the model it rests on: the run
kinds and states and who may change them, a worker's status, the event kinds, which steps are ready, the prompts a
run gives its agent, and the decisions and notices of a plan run. It needs only the standard library, so the api and
the daemon share it. Schema 0009 holds the tables, and 0010 adds plan runs, decisions and notifications, and `evo_agents/hub/server/workers.py` the routes that register and stop workers (pairings, join,
direct registration, list, drain, undrain, revoke). `evo_agents/hub/server/runs.py` holds the queue: ready steps,
dispatch, claim, heartbeat, state reports, the list of runs, and the owner's cancel, approve, rerun, takeover and
handback; `evo_agents/hub/server/run_state.py` moves runs, records each move in the plan, and holds the reaper and the
pruning of events; `evo_agents/hub/server/run_events.py` holds a run's events, their stream, the owner's messages and
the log and diff the worker uploads; `evo_agents/hub/server/terminal.py` relays the web terminal, whose frames and
close codes `evo_agents/hub/terminal.py` holds for the api and the daemon alike. `evo_agents/worker` is the daemon,
`evo-agents worker` (see [The daemon](#the-daemon)), with an adapter for each of the three runtimes in
`evo_agents/worker/runtimes` and its end of the web terminal. The daemon has its own command group because `evo-agents
hub worker` is already the server's job worker (see `docs/hub.md`). `docs/notifications.md` describes how decisions
and notices reach a member.

## Entities

| Entity | What it is | Table |
| --- | --- | --- |
| worker | a machine its owner registered: a name unique per owner, host facts (hostname, OS, arch, daemon version), 1 to 8 slots, labels, the runtimes and checkouts it reports, whether it allows the web terminal | `workers` |
| worker project | a project the worker may take runs of, chosen at registration | `worker_projects` |
| pairing | a one-time code the web creates so a machine can join without a machine token | `worker_pairings` |
| run | one attempt at one plan step on one worker (kind `step`), or one session on one worker that does every step of a plan not done yet (kind `plan`) | `runs` |
| run event | one entry of a run's log, numbered within the run | `run_events` |
| inbox message | a message from the owner to the run's agent, waiting for the worker | `run_inbox` |
| decision | a question a plan run's agent asks its owner, with 2 to 6 options | `decisions` |
| notification | a decision or a notice (a push to a default branch, say) for the run's owner, and its deliveries to the owner's channels | `notifications`, `notification_channels`, `notification_deliveries` |

A run records the project, plan and step key, the step's title at dispatch, the plan revision it was dispatched from,
who dispatched it, the worker (or the worker it is pinned to), the runtime the dispatch asked for
(`requested_runtime`: `claude-code`, `opencode`, `codex` or `any`, which the next attempt asks for again) and the one
the run has (`runtime`: the same, except that `any` becomes the runtime the claiming worker picked), the
mode (`headless` or `interactive`), the approval (`auto` or `review`), a timeout of 5 to 240 minutes, its attempt out
of at most 3, the run it retries (`parent_run_id`), its state and lease, the agent's session id, the repo and branch,
and at the end the commit, diffstat, verify results, evidence, usage and error.

## Who may do what

- **A worker belongs to the member who registered it, and only that member dispatches to it.** A claim takes only
  runs whose dispatcher owns the worker, and a dispatch that names a worker must name one of the caller's own (403
  otherwise). Another writer of the same project never gets a run onto your worker. A hub admin sees every worker
  and may drain or revoke one, but cannot dispatch to it or undrain it: only the owner sets a drained worker going
  again (403 for anyone else).
- **Roles on the project still apply.** Dispatching needs the writer role; reading runs, events and diffs needs
  reader. Messages, takeover, handback, cancel, approve, rerun and the terminal belong to the run's owner, the member
  who dispatched it. A worker takes runs only of the projects it was registered for, and only while its owner still
  holds writer on them: once the grant goes, claims skip those runs.
- **The agent has the full permissions of the machine's owner.** Claude Code runs with permission mode
  `bypassPermissions` (`--dangerously-skip-permissions`), opencode with the equivalent of `--auto`, and Codex with
  `--dangerously-bypass-approvals-and-sandbox`. The run's worktree is where the agent works, not a sandbox: the
  agent can read and change anything its owner can, and it runs on the owner's runtime accounts and quotas. In a run
  of one step the daemon refuses to push the default branch or a detached HEAD and never merges. In a plan run the
  agent may push, and merge into, the branch the plan names for a repo, the repo's default branch included, never
  forced, and each such push or merge sends the owner a notice.
- **A worker token (`evw_...`) works only on `/v1/worker/*`**, and machine tokens and web sessions get 403 there. The
  hub shows the token once, when the machine joins or registers, and keeps only its SHA-256. With it a worker reads
  nothing beyond the runs it holds. Revoking the worker ends the token at once, and revoking the token
  (`DELETE /v1/tokens/{id}`, or `DELETE /v1/admin/tokens/{id}` for a hub admin) revokes the worker with it, in the
  same transaction: its runs are released and its name is free again, as with `POST /v1/workers/{id}/revoke`.

What stays a risk: a plan's text, or anything the agent reads while it works, can carry instructions, and the agent
acts on them with the owner's permissions. Whoever controls the hub server itself, rather than an admin account
through the API, can hand work to every worker; signing dispatches with a key kept on the owner's machine is an open
question. The web terminal is a remote shell into the machine, which is why it has the extra checks under
[Terminal](#terminal). Every dispatch, cancel, approve, rerun, pairing, join, registration, drain, revoke and terminal
session leaves an audit row (`run.*`, `worker.*`, `terminal.open`, `terminal.close`), and so does each message,
takeover and handback (`run.message`, `run.takeover`, `run.handback`); a row names the run and the message by id,
never the text.

## Worker status

A worker's status follows from three columns and the clock (`worker_status` in `runs.py`), checked in this order:

| Status | When | Claims runs |
| --- | --- | --- |
| `revoked` | `revoked_at` is set; its token no longer works | no |
| `offline` | no heartbeat in the last 300 seconds, or none yet | no |
| `draining` | `drained_at` is set; it finishes the runs it holds | no |
| `online` | otherwise | yes |

The daemon sends a heartbeat every 15 seconds, so a worker is offline after about 20 missed heartbeats. Offline
comes before draining, since a drained worker that is down finishes nothing. The web shows an online worker as idle
or busy depending on whether it holds runs.

## Run states

| State | Meaning |
| --- | --- |
| `queued` | waiting for a worker to claim it |
| `leased` | claimed; the worker prepares the checkout |
| `running` | the agent works headless |
| `interactive` | a person drives the agent in a terminal (takeover, or interactive mode from the start) |
| `verifying` | the agent has finished; the daemon runs its verify commands again |
| `waiting` | a plan run whose agent ended its turn with a decision open; the worker keeps the run and the session |
| `review` | verified; waits for the owner to approve |
| `parked` | a plan run that waited 24 hours for an answer; the worker let it go and kept the session and worktrees |
| `done` | approved, or verified with approval `auto` |
| `failed` | the agent, the checkout or a verify command failed, the run timed out, or the last attempt's lease ran out |
| `lost` | its worker stopped extending the lease, and another attempt was queued |
| `cancelled` | stopped on the owner's request |

The worker holds a run, and extends its lease, while it is `leased`, `running`, `interactive`, `verifying` or
`waiting`; a waiting run keeps its slot. A run in any state before `done` is active, `parked` included, and a step has
at most one active run, as a plan has at most one active plan run. `done`, `failed`, `lost` and `cancelled` are final:
running the step again (rerun, or the next attempt) creates a new run, and so does resuming a parked plan run.

Each move has its actors (`TRANSITIONS` in `runs.py`); the hub refuses any other move, and refuses an actor the move
does not name. The `worker` is the worker that holds the run, the `owner` is the member who dispatched it, and the
`reaper` is the hub acting on its own.

| From | To | Who |
| --- | --- | --- |
| `queued` | `leased` | worker (claim) |
| `queued` | `cancelled` | owner |
| `queued` | `failed` | reaper, when nobody can claim the run any more, for example because its pinned worker was revoked |
| `leased` | `running`, `interactive` | worker |
| `running` | `interactive` (takeover), `verifying` | worker |
| `interactive` | `running` (handback), `verifying` | worker |
| `verifying` | `done` (approval `auto`, every verify command exited 0), `review` (approval `review`) | worker |
| `running` | `waiting` (the agent's turn ended with a decision open) | worker |
| `waiting` | `running` (the answer reached the agent) | worker |
| `waiting` | `parked` (no answer within 24 hours) | reaper |
| `review` | `done` (approve), `cancelled` | owner |
| `parked` | `done` (the owner answered, and the run that resumes it is queued) | owner |
| `parked` | `cancelled` | owner, or the reaper after 7 days |
| `leased`, `running`, `interactive`, `verifying`, `waiting` | `failed`, `cancelled` | worker or reaper |
| `leased`, `running`, `interactive`, `verifying`, `waiting` | `lost` | reaper |

A claim and each heartbeat set a held run's lease to expire 300 seconds later. Every minute the reaper
(`hub.recover_runs`) looks for held runs whose lease has expired. Such a run becomes `lost`, and the hub queues a
new run for the same step with `parent_run_id` pointing back and the attempt one higher; a third attempt becomes
`failed` instead, and a run the owner had asked to cancel becomes `cancelled`. Revoking a worker does the same at
once to the runs it holds; a held run pinned to that worker fails rather than coming back, and so does a queued run
pinned to it, since no other worker may claim either. Each of these moves writes a `state` event with the actor
`reaper`. Cancelling a held run sets `cancel_requested_at`; the next heartbeat tells the worker, which stops the agent
and reports `cancelled`.

The reaper also ends a held run that ran past its timeout, counted from when the agent started, or from the claim
while it has not (from 0.4.0, only the time a run spends `leased`, `running`, `interactive` or `verifying` counts,
which `run_seconds` in `runs.py` adds up, so waiting for a decision and being parked never time a run out): such a run ends `failed` with the error `it ran past its timeout of N minutes` (or `cancelled`, when
its cancel was asked for) and is not tried again, and the next heartbeat tells the worker to stop it. The reaper looks at
timeouts before leases, so a run both past its timeout and without a lease fails rather than coming back.

The hub writes the plan as the member who dispatched the run, through the same item update as `evo harness step`
(PATCH's own write), with `if_revision`, retrying up to 5 times on a revision conflict; each try reads the plan again.
When a run starts (`leased` to `running` or `interactive`), the step becomes `in_progress` with the note
`run #N on worker W`. When it ends `done`, the step gets `done`, `done_at` (the UTC date) and `evidence` in one
revision: the run, the worker, the runtime and attempt, `repo@commit` on the branch with the diffstat, each verify
command with its exit code, and the agent's summary, at most 16 KiB. When a run ends `failed` or `cancelled`, the step
goes back to `pending` with a note naming why (`run #N failed: ...`, `run #N was cancelled: ...`). A `lost` run leaves
the step alone, since its next attempt is queued at once; the last attempt fails rather than being lost. A step that
is `done` already is never set back. The plan write is the step's record, not the run's truth: it runs in a savepoint
of the move's transaction, so a dispatcher who lost the writer role, a plan that is gone, or 5 conflicts in a row leave
the move in place and a warning in the log. Each write is a `plan.patch` audit row of the dispatcher, with the worker's
token when the worker reported the move.

Revoking a worker, or its token, writes the steps of the runs that ended the same way, in the same transaction.

## Which steps are ready

A step is ready when its status is `pending` and every id in its `depends_on` names a step whose status is `done`
(`ready_steps` in `runs.py`). Ids compare as text, so `5` and `"5"` name the same step. A step without a status
counts as pending. A step whose status is `blocked`, `in_progress` or `done` is not ready, and neither is a step that
depends on a blocked step or on an id the plan does not have. `unready_reason` says why a step is not ready, in words
the web can show next to it.

The `blocking` flag is not read. execute-plan does not read it for its frontier either, from its 0.4.0 on: the flag
marks a step the plan cannot finish without, not a gate on the steps after it. A plan where every step is blocking
still runs every step that `depends_on` lets run.

Dispatch also refuses, with 409, a step that already has an active run; that check needs the database, so it is not
part of `ready_steps`.

## The prompt

`build_prompt(plan, step, repo)` gives the agent, in this order: rules, the repo and branch, the plan's goal and its
context (shortened), the step's title, `what`, `verify` and `note`, and the title and evidence of each step it
depends on. The rules tell the agent to:

- do this step only;
- leave the plan alone: no `plan_step` tool of the hub, no `evo harness step`, no `evo-agents hub plan`, since the hub
  records the step from what the run reports;
- commit in its checkout on the branch it is on, and not switch branches, merge or push (the daemon pushes);
- write `.evo-run/result.json` before it finishes, and keep `.evo-run/` out of its commits:

```json
{"verify_commands": ["python -m pytest -q tests/hub/test_runs_model.py", "ruff check ."], "summary": "..."}
```

`verify_commands` are the shell commands the agent ran to check the step. The daemon runs each of them again in the
checkout while the run is `verifying`, and approval `auto` marks the step done only when every one exits 0: the plan's
own `verify` is often prose mixed with commands, and a result the daemon saw is worth more than the agent's word.

The prompt is at most 32 KiB of UTF-8. Each part has its own budget (goal 2 KiB, context 4 KiB, what 10 KiB, verify
3 KiB, note 2 KiB, 1.5 KiB of evidence per dependency and 5 KiB for all of them, 200 bytes for a title, key, repo or
branch). A part over its budget is cut on a character boundary and ends with a mark saying how many bytes were left
out and that the plan on the hub has the full text. 0.4.0 gives a run of one step the same prompt as 0.3.0, byte for
byte (`tests/hub/golden/step-prompts-0.3.0.json`).

### The prompt of a plan run

`build_plan_prompt(plan, repos, worktrees)` gives the agent of a plan run, in this order: rules, each repo of the run
with its branch and worktree, the plan's goal and context (shortened as above), and one line per step not done yet
(key, status, title, repo and `depends_on`). The steps' what, verify and note stay in `.evo-run/plan.yaml`, which the
daemon writes above the worktrees, so a plan of any length fits in 32 KiB: a plan of 60 steps takes about 18 KiB, the
step list has 14 KiB, and steps past it are counted on a last line that points to the file. The rules tell the agent
to:

- use the execute-plan skill when its runtime has it, and otherwise work the same way: a step is ready when it is
  pending and every step it depends on is done, and checkpoint steps are the agent's too;
- read the plan as the hub holds it now with `evo-agents worker plan` before each step;
- record progress only with `evo-agents worker step` (`in_progress`, `done` with evidence and verify commands, which
  the worker runs again and refuses done on a non-zero exit, or `pending`), never with the hub's `plan_step` tool,
  `evo harness step` or `evo-agents hub plan`;
- ask the owner with `evo-agents worker ask` only a decision of the categories in `docs/notifications.md`, decide
  everything else itself and write each choice, with why, in the step's evidence on a line starting with `Decision:`;
- commit in each worktree on its branch, push or merge only into the branch the plan names for that repo (its default
  branch included), never force, and run `evo-agents worker notify` after each push or merge into a default branch it
  makes itself;
- keep `.evo-run/` out of its commits, and write `.evo-run/result.json` with a `summary` when it stops. A plan run has
  no verify commands at its end, since `evo-agents worker step` ran each step's.

## Plan runs

A plan run is the whole of a plan handed to one worker. Its owner picks the worker (one of their own, holding a
checkout of every repo with a step not done), the runtime, the model, the mode and a timeout of 2, 4, 8 or 24 hours
(`PLAN_TIMEOUT_CHOICES`), which counts only the time the agent runs. The run has kind `plan`, no step key, and the
list of its repos with the branch the plan names for each. A plan has at most one active plan run, and while it has
one, dispatching one of its steps gets 409, as does a second plan run; a plan run waits, likewise, until no run of one
of its steps is active.

On the worker the run gets a directory with a worktree of each of its repos on that repo's branch, and the agent works
in that directory with the plan in `.evo-run/plan.yaml`. The agent does the steps in the order `depends_on` allows,
the way execute-plan does, and reports each through `evo-agents worker step`: the worker runs the step's verify
commands again in the repo's worktree, commits and pushes that repo's branch, and the hub writes the step
`in_progress`, `done` with its evidence, or back to `pending`, as the member who dispatched the run. A run that ends
`done` writes no step itself; one that fails, is cancelled, or is lost for the last time gives the steps it left
`in_progress` back as `pending` with a note.

The agent talks to the hub through four commands that use the worker's token and the run's id (`EVO_RUN_ID`), not a
token of the owner's: `evo-agents worker step`, `ask`, `notify` and `plan`. Outside a run they refuse to run.

`POST /v1/projects/{p}/plan-runs` (writer) takes `plan_id`, `worker_id` (optional, one of the caller's own workers:
403 for any other id, 409 for a revoked worker or one that does not serve the project), `runtime` (`any` by
default), `model` (optional, one line of at most 200 characters; null leaves the choice to the runtime), `mode`
(`headless` by default) and `timeout_h` (2, 4, 8 or 24; 4 by default), and answers 201 with the run. It answers 409,
and queues nothing, when the plan has no pending step, when the plan has an active run of either kind, or when a step
that is not done names no repo and the plan does not list exactly one. The run has kind `plan`, approval `auto`, the
plan's title, no step key and no repo, and `repos`: the repo of each step not done, once each in plan order, with the
branch the plan's `repos` names for it (null when it names none). A dispatch of steps (`POST .../runs`, and a rerun)
and a dispatch of a plan run take the same transaction-scoped advisory lock of the plan before they look at its
active runs, so they cannot both get in: a step of a plan with an active plan run gets 409 (`plan X has plan run #N,
...`), and so does a plan run while a run of one of its steps is active. `ready-steps` answers the plan's active plan
run as `plan_run`, and while there is one, no step reads as ready. A plan run is not rerun (409): dispatch the plan
again.

The worker holding a plan run reads the plan as the hub holds it now with `GET /v1/worker/runs/{id}/plan` (as the
member who dispatched the run, in the shape of `GET /v1/projects/{p}/plans/{plan}`), and reports each step with
`POST /v1/worker/runs/{id}/steps/{key}`:

```json
{"status": "done", "repo": "evo-agents", "commit_sha": "<40 or 64 hex>",
 "verify": [{"command": "ruff check .", "exit_code": 0}], "evidence": "what the agent did and how it checked it"}
```

`status` is `in_progress`, `done` or `pending`; `repo` must be one of the run's repos (422 otherwise) and defaults to
the one the plan gives the step. `done` needs at least one verify result and every one exited 0 (422 otherwise). The
hub writes the step as the member who dispatched the run, through the same write as a run of one step: `in_progress`
with the note `run #N on worker W`, `pending` with the note `run #N on worker W handed it back`, and `done` with
`done_at`; the evidence (the run, `repo@commit` on the repo's branch, each verify command with its exit code, then the
agent's text) on `done`, or whenever the report carries evidence. A step that is done is never set back (409), and a
done step reported done again writes nothing (`written: false`). When the plan cannot be written (the dispatcher lost
the writer role, the plan is gone, five revision conflicts in a row) the report gets that error and nothing is kept.
The answer is `{run_id, plan_id, step_key, status, revision, written}`. Each report that writes leaves a `system`
event in the run's log (`{"text": "step 2: done (evo-agents@...)", "step_report": {step, status, repo,
commit_sha}}`) and a `run.step_report` audit row of the dispatcher with the worker's token. A step the plan does not
have, a run of one step, and a run the worker does not hold get 404.

A plan run's own moves write no step: starting it writes nothing, and it ends `done` from `verifying` without verify
results, since each step was verified when it was reported (it never goes to `review`: 409). One that ends `failed`
or `cancelled` sets back to `pending` the steps it reported, it or an earlier attempt of it, that are still
`in_progress`, with the note `run #N failed: ...` or `run #N was cancelled: ...`; a step someone else left in
progress stays as it is. A plan run that is `lost` queues its next attempt as a plan run with the same repos and model,
and leaves the steps as they are.

## Decisions and notices

A plan run's agent stops for its owner only on a decision of one of the categories `docs/notifications.md` lists
(deploy, deleting data, a migration on real data, sending to a service outside, spending money, an architectural or
scope choice the plan leaves open), with `evo-agents worker ask`. It goes on with work that does not depend on the
answer, and when none is left it ends its turn: the run is `waiting`, the worker keeps it with its slot and lease, and
the owner sees the decision in the Inbox on the web. The answer goes to the run's inbox as a message naming the
decision, the worker hands it to the agent in the same session, and the run is `running` again.

A run that waits 24 hours (`DECISION_WAIT_SECONDS`) is `parked`: the next heartbeat tells the worker to stop the agent
at the end of its turn, the slot is free, and the worker keeps the agent's session and the worktrees. An answer then
queues a new plan run pinned to the same worker, with `resume_of_run_id` naming the parked run, which goes on in the
same session and worktrees; the parked run is `done`. A run parked for 7 days (`PARKED_DAYS`) is `cancelled` and its
decisions expire.

A push or merge into a repo's default branch, a plan finished and a plan run failed are notices: the owner reads them
in the Inbox and answers nothing. Decisions and notices both reach the owner as notifications, through the channels
of `docs/notifications.md`.

## Protocol, version 1

Plain HTTP calls, each a request and one answer, so any proxy carries them. The design borrows from Woodpecker
(claim, extend, wait, log, done), from Forgejo's runner (log rows with an index the server acknowledges) and from
Multica (a wake-up plus a catch-up poll).

### Version header

Every request to `/v1/worker/*`, the public join included, carries `X-Evo-Worker-Protocol: 1`. A request with
another value, or without the header, gets 426 (`upgrade_required`) in the hub's usual error shape before any
credential is looked at, so an old daemon learns that it must be upgraded instead of misreading answers. Routes for
members do not use the header.

### Joining

The owner creates a pairing on the web (`POST /v1/workers/pairings`, with the session's CSRF header, or with a
machine token), naming the worker, its projects (each one the owner holds writer on; 403 otherwise), slots, labels
and whether it allows the web terminal. The code is 8 characters of Crockford base32 written `XXXX-XXXX`; it lasts 10
minutes, and a member has at most 5 codes that are neither used nor expired (409 for a sixth); a locked code counts
until it expires, so locking codes does not free a place. The hub keeps the HMAC-SHA256 of the whole code under
`EVO_HUB_SESSION_SECRET` and, in clear, its first four characters, the selector, which no two unused pairings share: a
join finds the pairing by its selector, so a wrong rest of the code counts as a wrong try against that pairing.
Whoever reads the table without the secret cannot test guesses of the rest against it. A hub without the session
secret answers 503 to pairing and joining, and changing the secret makes the codes waiting at that moment useless. An
unused pairing is deleted a day after it expired.

The web polls `GET /v1/workers/pairings/{id}` until the machine joins; the answer says `waiting`, `joined`, `expired`
or `locked` and how many wrong tries are left. On the machine, the daemon sends the code (any case, with or without
the hyphen; O reads as 0, I and L as 1) and its host facts (`hostname`, `os`, `arch`, `agent_version`) to
`POST /v1/worker/join`, the one public worker route, and gets its `evw_` token once. Five wrong tries lock the code,
and the right code is refused after them. An unknown, wrong, expired, used or locked code gets the same 403, so the
answer tells nothing about which codes exist; a code that is not 8 characters of the alphabet gets 422 and counts
against nothing. After 10 refused codes (403) from one client address within 10 minutes, every join from that
address gets 429 with `Retry-After` until the oldest refusal is 10 minutes old. The count lives in the api process's
memory, and the address is the one the reverse proxy forwards only when `EVO_HUB_FORWARDED_ALLOW_IPS` lists that
proxy (`docs/hub.md`); otherwise every join through the proxy shares the proxy's address and its count. When the
owner has lost the writer role on one of the pairing's projects since, or has a live worker of that name by then,
the join gets 409 and the code is not used up. A machine already signed in with
`evo-agents hub login` can register directly instead (`POST /v1/workers` with its machine token; a web session gets
403 there).

### Claim

`POST /v1/worker/claim` waits up to 25 seconds for a run; the body `{"wait_s": 10}` asks for a shorter wait, and may
be left out. The hub listens on the Postgres channel `evo_runs`, which each dispatch, rerun and next attempt notifies
with the run's id, on one connection of the api process opened by the first claim, and also looks again every 5
seconds, so a missed notification delays a claim but never loses one. It picks a queued run with `SELECT ... FOR UPDATE SKIP LOCKED` when all of these hold: the worker's owner
dispatched it, the project is one of the worker's and the owner still holds writer on it, the worker reported the
runtime as available (a run asking for `any` takes the first of `claude-code`, `opencode`, `codex` it has) and a
checkout of the repo, the run is pinned to no other worker, the worker holds fewer runs than its slots, and it is
neither draining nor revoked. The oldest such run is leased for 300 seconds (`EVO_HUB_RUN_LEASE_SECONDS`, which
tests shorten). The answer is `{"run": {...}}` with the
id, project, plan, step key and title, plan revision, attempt, max attempts, parent run, runtime, mode, approval,
timeout in minutes, repo, branch, lease expiry and the prompt (built from the plan revision the run was dispatched
from), with the run's `kind` and `model`, or `{"run": null}` when the wait ends empty, and the daemon claims again at
once. A plan run is claimed only by a worker with a checkout of every repo in its `repos`; its answer has no step key
and no repo, but `repos`, the prompt of `build_plan_prompt`, and `plan`, `{revision, body}` at the hub's current
revision, which the daemon writes to `.evo-run/plan.yaml`. A worker has at most one
claim waiting: a newer claim ends the older one, which answers no run. A claim whose worker hung up, as a daemon that
stops drops the claim it waits on, takes no run: it ends before it looks at the queue again, and when the worker hangs
up while the claim leases a run, the lease is rolled back before it commits, so the run stays queued for the next
claim instead of waiting out a lease nobody holds.

### Heartbeat

Every 15 seconds the daemon sends `POST /v1/worker/heartbeat` with its runtimes, checkouts, free slots and the runs
it holds:

```json
{
  "runtimes": {
    "claude-code": {"available": true, "version": "2.1.289"},
    "codex": {"available": false, "version": "0.153.4", "reason": "not signed in"}
  },
  "checkouts": {"evo-agents/evo-agents": {"path": "/Users/me/github/evo-agents", "branch": "main"}},
  "free_slots": 1,
  "runs": [12],
  "agent_version": "0.3.0"
}
```

`runtimes` is keyed `claude-code`, `opencode` or `codex`, each `{available, version, reason}`: `available` is
required, `reason` says why a runtime that is there cannot take runs, and only an available runtime is claimed for.
`checkouts` is keyed `<project>/<repo>`, as the project and its repo are named on the hub, each `{path, branch}` with
the path required. The hub keeps both as sent, every key present (a missing one is null), and `GET /v1/workers/{id}`
shows them in that shape. `free_slots` is what the daemon counts free, at most the worker's slots; the hub shows it,
and counts the runs a worker holds against its slots itself. `agent_version` is optional and replaces the version
the worker registered with.

The hub records the heartbeat, extends the lease of each run named that the worker still holds by 300 seconds (the
same `EVO_HUB_RUN_LEASE_SECONDS`), and answers with control:

```json
{
  "drain": false,
  "runs": [{"id": 12, "held": true, "state": "running", "lease_expires_at": "...", "cancel": false,
            "takeover": false, "handback": false, "terminal_open": false, "inbox": 0}]
}
```

For each run: whether to `cancel` (the owner asked for it), `takeover`, `handback` or open the terminal
(`terminal_open`), and how many `inbox` messages wait; for the worker, whether to `drain`. A run the worker reports
but no longer holds (lost, cancelled, or another worker's now) comes back with `held: false`, `state: null` and
`cancel: true`. `takeover` stays true from the owner's ask until the worker reports `interactive`, and `handback`
until it reports `running` (see [Takeover and handback](#takeover-and-handback)); `terminal_open` is true while a
browser waits for the worker's end of the run's terminal (see [Terminal](#terminal)).

### State

`POST /v1/worker/runs/{id}/state` reports a move of a run the worker holds, with the commit, diffstat, verify results
and usage when it has them:

```json
{"state": "done", "from": "verifying", "commit_sha": "<40 or 64 hex>", "session_id": "...",
 "diffstat": {"files": 3, "insertions": 120, "deletions": 4},
 "verify": [{"command": "ruff check .", "exit_code": 0, "duration_ms": 900}],
 "usage": {"input_tokens": 1200}, "error": null, "summary": "what the agent says it did"}
```

The hub checks the move against the transition table with the worker as actor and answers 409 when the table refuses
it, and 404 when the worker does not hold the run (another worker's, or one no longer held: lost, cancelled, in
review or done). `from`, when given, must be the state the run is in (409 otherwise). `done` needs approval `auto`
and at least one verify result, every one with exit code 0; `review` needs approval `review`; otherwise 409. A plan
run ends `done` without verify results and never reports `review`. A `failed` report without an `error` gets one naming the worker. Reporting the state the run is in already moves
nothing and keeps the session id, commit, diffstat, verify results and usage it carries, so a resend after a lost
answer is safe. The answer is the run as the hub holds it.

### Events

`POST /v1/worker/runs/{id}/events` sends a batch of at most 500 events and 1 MiB of request (413 with the detail
`{"limit": "batch_bytes"}` beyond, before anything is parsed). Each event has `seq`, `at` (with its time zone; a time
after the hub's clock is stored as the hub's now), `kind` (one of the worker's kinds below: `user_message` and `state`
get 422) and `body`, a JSON object:

```json
{"events": [{"seq": 41, "at": "2026-10-05T09:12:03.120Z", "kind": "agent_message_chunk", "body": {"content": {"type": "text", "text": "..."}}}]}
```

`seq` is the worker's own count for the run: 1 for the first event and one more for each next. The hub remembers the
highest `seq` it has stored with none missing below it and answers with it as `ack_seq`, with how many events of the
batch it `stored`; the daemon then deletes its spool up to there. The hub takes a batch in `seq` order. An event at
or below `ack_seq` is a resend and is skipped, so a batch can always be sent again safely; one that comes after a gap
is not stored, and neither is anything after it, so the daemon sends again from `ack_seq + 1`. The worker that claimed
the run sends its events while it holds the run and after (its spool may still hold some when the run ends; it sends
them before the report that ends the run); any other worker gets 404.

The hub numbers what it stores itself: `run_events.seq` counts all of the run's events, those from the worker and
those the hub writes (`user_message` when the owner sends a message, `state` on each move), and is what
`events?after=SEQ` and the SSE stream's `Last-Event-ID` refer to. The number is given under the run's row lock, so a
reader never sees an event before the ones numbered below it. A body whose JSON is over 64 KiB is cut and the event
marked `truncated`: its longest strings end early with the mark `[cut by the hub: the event was over 65536 bytes]`, so
its keys stay, and a body of many short values becomes `{"cut": "<the start of its JSON>"}`. A run keeps at most
20,000 events of the worker and the owner: a batch that would pass it stores nothing and gets 413 with the detail
`{"limit": "events_per_run", "max": 20000, "ack_seq": N}`, and the worker should send no more of that run's events.
The hub's own `state` events are written past the limit, since a move must never fail for it.

### The inbox

The owner's messages wait in the run's inbox until the worker takes them: the heartbeat counts them (`inbox`), and
`POST /v1/worker/runs/{id}/inbox` with `{"ack": ID}` marks the messages up to `ID` delivered, the ones the daemon
handed to the agent, and answers with those still waiting, oldest first, at most 100 (`{"messages": [{id, text,
sent_by, created_at}]}`). The body may be left out to read without acknowledging. Only the worker holding the run
reads its inbox (404 otherwise).

### Log and diff

When the run ends, the worker uploads the whole log as blob kind `run-log` (at most 64 MiB) and the diff of the run's
commits as `run-diff` (at most 8 MiB), through the blob store's own uploads and commit, as its owner, who must still
hold writer on the project. `POST /v1/worker/runs/{id}/uploads` takes `{"items": [{sha256, size, kind}]}`, one item of
each kind at most (another kind gets 422, a size over the kind's limit 413), and answers as `POST /v1/blobs/uploads`:
a presigned PUT per blob the project does not hold yet. The worker PUTs the bytes, then `POST
/v1/worker/runs/{id}/blobs` with `{"upload_ids": [...]}` checks and commits them as `POST /v1/blobs/commit` does (an
upload of another kind is unknown, 422) and records them on the run (`log_sha256`, `diff_sha256`). Only the worker
that claimed the run uploads for it.

### When the hub does not answer

The run keeps going. The daemon writes events to a spool on disk (at most 256 MiB) and sends them later by `seq`;
a failed call is retried with a backoff that grows from 1 to 60 seconds. A daemon that cannot heartbeat for 300
seconds loses its leases, and the reaper takes over from there.

## Routes

For the worker, with an `evw_` token and the version header:

| Route | What it does |
| --- | --- |
| `POST /v1/worker/join` | trades a pairing code for a token (public) |
| `POST /v1/worker/claim` | waits up to 25 s for a run |
| `POST /v1/worker/heartbeat` | reports the machine, extends leases, returns control |
| `POST /v1/worker/runs/{id}/state` | reports a move |
| `GET /v1/worker/runs/{id}/plan` | reads the plan of a plan run it holds, as the hub holds it now |
| `POST /v1/worker/runs/{id}/steps/{key}` | reports a step of a plan run it holds |
| `POST /v1/worker/runs/{id}/events` | sends a batch of events |
| `POST /v1/worker/runs/{id}/inbox` | acknowledges messages handed to the agent, takes the waiting ones |
| `POST /v1/worker/runs/{id}/uploads`, `/blobs` | uploads the run's log and diff, records them on the run |
| websocket `/v1/worker/runs/{id}/terminal` | the worker's end of the terminal |

For members, with a web session or a machine token. `{p}` is a project, and run routes sit under
`/v1/projects/{p}/runs/{id}`:

| Route | Who | What it does |
| --- | --- | --- |
| `POST /v1/workers/pairings`, `GET /v1/workers/pairings/{id}` | member | create a pairing code, follow it |
| `POST /v1/workers` | member (machine token) | register this machine directly |
| `GET /v1/workers`, `GET /v1/workers/{id}` | owner; a hub admin sees all | list and show workers |
| `POST /v1/workers/{id}/drain`, `/revoke` | owner or hub admin | stop new claims, end the worker |
| `POST /v1/workers/{id}/undrain` | owner | resume claims |
| `GET /v1/projects/{p}/plans/{plan}/ready-steps` | reader | every step, with whether it may be dispatched and why not |
| `POST /v1/projects/{p}/runs` | writer | dispatch steps, all or none |
| `POST /v1/projects/{p}/plan-runs` | writer | dispatch a plan run: every step of the plan not done yet, on one worker |
| `GET /v1/projects/{p}/runs`, `GET .../runs/{id}` | reader | list (filters, pages, counts by state) and show runs |
| `GET .../runs/{id}/events?after=SEQ` | reader | events after a number |
| `GET .../runs/{id}/stream` | reader | the same as server-sent events, with a ping every 15 s, resumed by `Last-Event-ID` |
| `GET .../runs/{id}/diff` | reader | a presigned URL of the run's diff |
| `POST .../runs/{id}/messages` | owner | a message for the agent, at most 8 KiB |
| `POST .../runs/{id}/takeover`, `/handback` | owner | switch between headless and interactive |
| `POST .../runs/{id}/cancel`, `/approve`, `/rerun` | owner | stop, approve a run in review, run the step again |
| websocket `/v1/projects/{p}/runs/{id}/terminal` | owner, on a worker of theirs | the browser's end of the terminal |

### Dispatching and the owner's controls

`POST /v1/projects/{p}/runs` takes `plan_id`, `steps` (1 to 50 step ids or orders), `runtime` (`any` by default,
or `claude-code`, `opencode`, `codex`), `mode` (`headless` by default), `worker_id` (optional: pin the runs to a
worker of the caller's), `approval` (`review` by default, or `auto`) and `timeout_min` (5 to 240, 60 by default). It
queues one run per step, at the plan's current revision, or nothing: a step that is not ready or has an active run is
409, a step the plan does not have or one without a repo is 422, a worker that is not the caller's (or no worker) is
403, and a revoked worker or one that does not serve the project is 409. `ready-steps` answers every step of the plan
in plan order with `ready`, `reason` (as `unready_reason` says it, or the active run) and the active run.

The owner of a run is the member who dispatched it; another member gets 403 and someone without a grant 404.
`.../cancel` moves a queued run, or one in review, to `cancelled` at once; for a held run it sets
`cancel_requested_at`, the next heartbeat says `cancel`, and the worker reports `cancelled` (an expired lease after
that ends the run `cancelled` too). `.../approve` moves a run in review to `done` and writes the step done with the
evidence the run got when it went to review. `.../rerun` queues the step of a run that ended again, at the plan's
current revision, with the same requested runtime, mode, approval, timeout and pinned worker, attempt 1 and
`parent_run_id` naming the old run; the step must be ready again. Approve and rerun need the writer role.

### Reading runs

A member with a grant on the project reads its runs, of the plans it may read through the sink it names
(`X-Evo-Sink`, the project's hub sink without one), by the plan's label as the hub holds it now: a run of a plan it
may not read, or of a plan no longer on the hub, reads as no run (404), and a hub admin without a grant gets 403.

`GET /v1/projects/{p}/runs` lists runs newest first. It takes `state` (repeated for several), `plan_id`, `step` (a
step key), `worker_id`, `dispatched_by` (a login, in any case) and `q`, text found in the step's title, the step key,
the plan, the repo, the branch, the worker's name, the dispatcher's login or the error (`%` and `_` taken literally),
or a run number such as `#12`; and `limit` (1 to 200, 50 by default) and `offset`. The answer has the page of
`runs`, the `total` that match every filter, and `counts`, the runs in each of the ten states that match the other
filters, every state present: what the summary cards and the state facet show, so picking a state does not change
the counts beside it. `GET .../runs/{id}` shows one run. A run carries the step's `title`, `last_seq` (its latest
event, 0 before the first), `log_sha256` and `diff_sha256`, and the owner's open asks (`cancel_requested_at`,
`takeover_requested_at`, `handback_requested_at`).

`GET .../runs/{id}/events?after=SEQ` answers the events after `SEQ` in seq order, at most `limit` (500 by default,
1,000 at most), only the kinds named when `kind` is given, with the run's `state`, its `last_seq` and whether `more`
follow. `GET .../runs/{id}/stream` sends the same events as server-sent events (`text/event-stream`, through FastAPI's
own `EventSourceResponse`): each one's `id` is its seq and its `data` the event as `events` answers it. A first
connection starts after `after` (0 by default), and a reconnecting browser's `Last-Event-ID` wins over it, so an
`EventSource` resumes where it stopped without a gap or a repeat. FastAPI sends a `: ping` comment after 15 idle
seconds, which keeps proxies from closing the stream, and the answer carries `Cache-Control: no-cache, no-transform`
and `X-Accel-Buffering: no`, so a proxy neither caches nor compresses it (a compressing proxy would hold the events
back until its buffer fills). Once the run is final and every event of it was sent, the stream
sends `event: end` with `{"state": ..., "last_seq": ...}` and closes; a client should stop there instead of
reconnecting. A stream holds no database connection while it waits: a notification on the Postgres channel
`evo_run_events`, which each write of a run's events sends with the run's id, wakes it, and it also looks again every
5 seconds, so a missed notification delays an event and never loses one. Every claim and stream of an api process
shares one listening connection.

`GET .../runs/{id}/diff` answers a presigned GET of the run's diff, working for 5 minutes, with its sha256 and size;
`download=true` asks the store to answer as the attachment `run-<id>.diff`. A run without a diff gets 404.

### Messages

`POST .../runs/{id}/messages` with `{"text": "..."}` (at most 8 KiB of UTF-8, not blank) leaves a message for the
run's agent, while the run is queued or held (409 otherwise: no agent would read it). The run's log gets a
`user_message` event `{"text", "from", "message_id"}` and the answer is the message with its `seq`. Only the owner
sends messages (403 for another member), and a message counts against the run's 20,000 events (413).

### Takeover and handback

`POST .../runs/{id}/takeover` asks the worker of a `leased` or `running` run to stop the agent at the end of its turn
and resume its session in a terminal, where a person drives it; `POST .../runs/{id}/handback` asks the worker of an
`interactive` run to let the agent go on headless in the same session. Each sets `takeover_requested_at` or
`handback_requested_at`, the next heartbeat says `takeover` or `handback`, and the worker's report of `interactive`
or `running` ends the ask; any other move out of the states it was asked in drops it too. A run in another state gets
409. Asking again while an ask is open changes nothing and writes no second audit row. Only the owner asks.

## Event kinds

The kinds take their names from the `session/update` notifications of the Agent Client Protocol (ACP), so the
adapters could move to ACP later at little cost, plus four of the hub's own.

| Kind | Written by | What it holds |
| --- | --- | --- |
| `agent_message_chunk` | worker | text from the agent |
| `agent_thought_chunk` | worker | the agent's reasoning, when the runtime shows it |
| `tool_call` | worker | a tool the agent started |
| `tool_call_update` | worker | progress or the result of that tool |
| `plan` | worker | the agent's own task list |
| `usage_update` | worker | tokens and cost, as the runtime reports them |
| `system` | worker | the daemon's own lines: checkout, verify commands and their exit codes, push |
| `output` | worker | a runtime event no adapter recognises, kept raw |
| `user_message` | hub | a message the owner sent |
| `state` | hub | a move between run states, with its actor |

The hub refuses `user_message` and `state` from a worker.

## Terminal

The web terminal joins two websockets in the api's memory, the browser's and the worker's
(`evo_agents/hub/server/terminal.py`). `evo_agents/hub/terminal.py` holds the frames and the close codes, with the
standard library only, so the daemon uses them too.

The HTTP middleware reads no credential of a websocket. It closes every websocket under `/v1` during the handshake,
which the client sees as a 403, except these two routes, which check their own credential; a websocket route added
later is refused until it does the same and is listed in `SELF_CHECKED_WEBSOCKETS` of
`evo_agents/hub/server/security.py`. Both ends accept the socket before they check anything, so the client gets the
close code: a page sees a websocket refused during its handshake only as 1006.

**The browser's end**, `/v1/projects/{p}/runs/{id}/terminal`, closes at the first check that fails, in this order:

1. an `Origin` equal to the origin of `EVO_HUB_PUBLIC_URL` (4403). It comes before anything about the session, so a
   page of another site learns nothing from the answer.
2. the session cookie (4401 without one).
3. the hello, a text message within 10 seconds (4408 otherwise): `{"csrf": "...", "cols": 120, "rows": 40}`, where
   `csrf` is the session's `X-Evo-CSRF` value from `GET /v1/auth/web/csrf` (4403 otherwise) and the size is optional,
   both or neither, each from 1 to 1000 (1003 otherwise).
4. a live web session (4401) created within the last 12 hours (4403).
5. a run the caller may read, which it dispatched, held by a worker it owns that allows the web terminal, and
   `leased`, `running` or `interactive` (4403 for each).
6. no other browser on the run's terminal (4409).

Opening the terminal of a `leased` or `running` run asks for a takeover as `POST .../takeover` does, with its
`run.takeover` audit row when it is a new ask, since a person drives the agent only while the run is `interactive`. The
session is audited as `terminal.open` in the same transaction.

**The worker's end**, `/v1/worker/runs/{id}/terminal`. While a browser waits, the heartbeat says `terminal_open: true`
for the run, so the daemon learns of it within 15 seconds. The daemon connects with the version header (4426 without)
and its `evw_` token as `Authorization: Bearer` (4401 without one, or for a token that is not live; 4403 for a machine
token or a web session). The worker must hold the run, the run must be `interactive`, so after a takeover the daemon
connects once it has reported `interactive`, and a browser must wait on the run (4403 for each); a second connection of
the worker's end gets 4409.

**Frames.** Every message after the hello is binary, at most 64 KiB with its type byte, and its first byte is its type,
as in ttyd:

| Byte | Type | From | Payload |
| --- | --- | --- | --- |
| 0 | input | browser | bytes typed or pasted |
| 1 | output | worker | bytes the terminal printed |
| 2 | resize | browser | columns, then rows: two unsigned 16-bit big-endian integers, each from 1 to 1000 |

When the worker's end connects, the hub first sends it the browser's size, from the hello or the last resize since,
then relays the frames as they come. Input the browser sends before that is dropped, so the web takes input once the
first output arrives; a resize before that only sets the size the worker gets first. A text message after the hello,
an empty frame or a type its end may not send closes the session with 1003, and a frame over 64 KiB with 1009.

**Ending.** A session ends when either end leaves (the other gets 1000), after 15 minutes without a frame either way,
or 4 hours after it opened (both get 4408). A run has one browser at a time; once the session ended, the next browser
may open it, and the daemon gets `terminal_open` again. The end is audited as `terminal.close` with the bytes the hub
sent each end and how the session ended, as in `<project>/<plan>#<step> run:12 to_worker=1520 to_browser=88211
end=idle`; no audit row holds a byte of what was typed or printed. On the worker, the daemon attaches a PTY to the
run's tmux session and keeps the last 256 KiB of output, which it replays when a browser connects again.

| Close code | When |
| --- | --- |
| 1000 | the other end left |
| 1003 | a malformed hello, a text message after it, an empty frame, or a type the end may not send |
| 1009 | a frame over 64 KiB |
| 1011 | the hub's database did not answer |
| 4401 | no session or worker token, or one that is revoked, expired or unknown |
| 4403 | an Origin of another site, or a credential that may not open this terminal |
| 4408 | no hello within 10 seconds, 15 idle minutes, or 4 hours |
| 4409 | the run's terminal is open in another browser, or its worker's end is connected already |
| 4426 | the worker sent another version of the worker protocol, or none |

**Routing.** Both websockets sit under `/v1`, so they reach the api the way every other route does (`docs/hub.md`).
In production the reverse proxy's path rule sends `/v1` to the api, and Traefik passes the websocket upgrade there
without more configuration. A stack without a proxy (`deploy/hub/docker-compose.dev.yml`, Playwright) goes through the
web's own `/v1` rewrite, and that carries them too: Next.js standalone forwards the upgrade to the api, which
`web/e2e/terminal.spec.ts` checks in Chromium and Firefox. The browser therefore always opens the terminal on the
page's own origin, under the CSP's `connect-src 'self'`.

## Limits

| What | Limit |
| --- | --- |
| claim wait | 25 s, looking again every 5 s besides the notifications |
| steps per dispatch | 50 |
| heartbeat | every 15 s |
| offline after | 300 s without a heartbeat |
| lease | 300 s (`EVO_HUB_RUN_LEASE_SECONDS`, 5 to 3600), extended by each heartbeat |
| attempts per dispatch | 3 |
| slots per worker | 1 to 8 |
| run timeout | 5 to 240 minutes; a plan run 2, 4, 8 or 24 hours of agent time |
| decision | 2 to 6 options, context 16 KiB; parked after 24 hours waiting, cancelled 7 days later |
| prompt of a plan run | 32 KiB, its list of steps 14 KiB |
| pairing code | 10 minutes, 5 unused per member (locked ones included until they expire), locked after 5 wrong tries |
| refused joins | 10 per client address in 10 minutes, then 429 |
| prompt | 32 KiB |
| event batch | 500 events and 1 MiB |
| event body | 64 KiB of JSON, longer ones cut and marked `truncated` |
| events per run | 20,000 of the worker and the owner |
| events per read | 500 by default, 1,000 at most |
| runs per page of the list | 50 by default, 200 at most |
| stream | a ping after 15 idle seconds, a look every 5 s besides the notifications |
| message to the agent | 8 KiB |
| inbox per read | 100 messages |
| run log, diff | 64 MiB, 8 MiB in the blob store |
| events kept | 30 days after the run ends (`EVO_HUB_RUN_LOG_DAYS`) |
| terminal | hello within 10 s, 64 KiB frames, 15 minutes idle, 4 hours, one browser per run, sessions under 12 hours old |
| daemon spool | 256 MiB |
| daemon worktrees | removed 7 days after their run ends |

## The daemon

`evo-agents worker` turns the machine into a worker. It needs the worker extra, `uv tool install 'evo-ak[worker]'`
(aiohttp, and the SDKs the runtime adapters use).

| Command | What it does |
| --- | --- |
| `evo-agents worker join --url URL --code CODE` | trades a pairing code from the web for a worker token |
| `evo-agents worker register --name NAME --project P [--project P ...] --slots N [--label L ...]` | registers the machine with the machine token of `evo-agents hub login`, without a code; `--allow-web-terminal` lets the owner open the terminal |
| `evo-agents worker run` | the daemon in the foreground |
| `evo-agents worker service install` | keeps the daemon running in the background, now and at each login (see [In the background](#in-the-background)) |
| `evo-agents worker service uninstall` | stops the daemon and removes the service; the worker stays registered |
| `evo-agents worker service status [--json]` | whether the service is installed and the daemon runs, its pid and last exit |
| `evo-agents worker status [--json]` | the worker as this machine and the hub see it: runtimes, tmux and whether the web terminal is allowed, checkouts, the daemon's pid, runs kept, the spool |
| `evo-agents worker attach N` | puts this terminal on the tmux session `evo-run-N` of an interactive run (`tmux attach`, or `switch-client` from inside tmux on the same server) |
| `evo-agents worker selftest --runtime NAME [--model M] [--effort E] [--keep]` | runs the runtime's adapter for real on a tiny prompt in a scratch git repository, prints each event and how many of each kind came, and exits 0 when the turn completed and the agent wrote the file it was asked for (it spends a little of the owner's quota; Claude Code and Codex run at effort `low` unless `--effort` says otherwise) |
| `evo-agents worker drain [--resume]` | stops claims (or resumes them) through `POST /v1/workers/{id}/drain` or `/undrain`, with the machine token |
| `evo-agents worker revoke [--force]` | revokes the worker on the hub and deletes its token here; `--force` deletes it even when the hub cannot be reached; a background service stays installed, does not start the daemon again, and is named with the command that removes it |

`join` and `register` refuse a machine that is a worker already, unless `--replace` is given. When the machine is
signed in to the same hub, they keep the repos of the worker's projects as the hub lists them, and `run` reads them
again at its start.

### State on the machine

Everything lives under `~/.evo/worker` (or `$EVO_WORKER_HOME`), mode 0700: `token` (0600) holds the `evw_` token,
which no command prints; `config.json` (0600) the hub, the worker as the hub registered it and the repos of its
projects; `worker.log` (0600) the daemon's JSON log lines, rotated at 10 MiB with five old files kept, with tokens,
pairing codes and presigned signatures masked; `service.log` what the daemon printed under launchd before its log
was open; `daemon.pid`, locked while a daemon runs, so a second one refuses to start; `spool/` the events not
acknowledged yet; `runs/<id>/` what the daemon knows of each run and its whole event log; `worktrees/` the runs'
worktrees.

### Runtimes and checkouts

The heartbeat reports each of `claude-code`, `opencode` and `codex`. A runtime counts as available when its adapter
finds it (by default its binary, `claude`, `opencode` or `codex`, on PATH, answering `--version`); a runtime whose
binary is there but that has no adapter in the installed release is reported unavailable with that reason, so the
hub hands it no run. Adapters are found by runtime name in the package, in the entry point group
`evo_agents.worker.adapters`, and in `EVO_WORKER_ADAPTERS` (`runtime=module:Class`, separated by commas), each later
source winning; `evo_agents/worker/adapter.py` holds the interface an adapter implements.

A checkout is keyed `<project>/<repo>` and found, the first source winning, in `checkouts` of `config.json` (set by
hand), in the project's repos as the hub lists them, placed in the workspace of the project's cluster in the harness
registry (else the workspace the project was registered with), and in the registry's cluster of the project
(`hub.project`, on the same hub), keyed by directory name. The registry is `~/.evo/harness/registry.json`, then
`~/.claude/harness/registry.json`, as `evo-agents hub registry pull` writes it. Only a git work tree counts.

### Runtime adapters

`evo_agents/worker/runtimes` holds an adapter for each runtime. Each one drives its runtime through the runtime's
official SDK or API, never by parsing what its CLI prints, and takes the state of a turn from the runtime's events,
never from an exit code. Each starts its runtime in a session of its own, through a small launcher that calls
`setsid`, so a Ctrl-C at the daemon's terminal does not reach the agent, and its process group, tools included, is
killed once the agent has ended, or 20 seconds after an interrupt it did not end on.

| Runtime | Through | Full permissions | A message of the owner | Interrupt | The turn ends |
| --- | --- | --- | --- | --- | --- |
| `claude-code` | `claude-agent-sdk`: `ClaudeSDKClient` over `claude` on PATH, session id made by the daemon (`--session-id`, or `--resume` for a run that goes on with a session) | `permission_mode="bypassPermissions"` (`--dangerously-skip-permissions`) | `query()`; Claude Code takes it at the next tool boundary and may fold it into the same `result` | the SDK's `interrupt()`, then the input stream ends | at its `result`: the input stream ends and the CLI exits once it has done what it was given; `stop_at_turn_boundary` ends the input stream at once, and the turn in progress still finishes |
| `codex` | `openai-codex` over `codex app-server --listen stdio://`, with `codex` on PATH | sandbox `danger-full-access` and approval policy `never` (`--dangerously-bypass-approvals-and-sandbox`) | `turn/steer` during the turn; a new turn on the same thread when the turn no longer takes it | `turn/interrupt` | at `turn/completed`, with its status |
| `opencode` | HTTP and the event stream of `opencode serve`, started per run on 127.0.0.1 at a free port, with a random `OPENCODE_SERVER_PASSWORD` and stdin `/dev/null` | each `permission.asked` of the session answered `once`, as `opencode run --auto` does, so the owner's explicit denials still hold; sessions get `opencode run`'s rules (no question, no plan mode) | `POST /session/:id/prompt_async`, taken at the next step boundary | `POST /session/:id/abort` | when the session goes idle with every prompt it was sent |

The agent gets, on top of its runtime's own system prompt, a note that it runs unattended and that the owner may send
messages. The model and the reasoning effort are the run's `model` and `effort` when the hub sends them, else
`EVO_WORKER_<RUNTIME>_MODEL` and `EVO_WORKER_<RUNTIME>_EFFORT` in the daemon's environment (`CLAUDE_CODE`,
`OPENCODE`, `CODEX`), else the runtime's default. opencode is always told the model: the run's, the variable's, or the
`model` of the owner's opencode config, and a model `opencode serve` does not list fails the start with that reason.
A message that comes once the agent takes no more input (its last turn is over) is refused by `send`, and stays in the
inbox.

The events take the shapes of ACP's `session/update`: `agent_message_chunk` and `agent_thought_chunk` carry
`{"content": {"type": "text", "text": ...}}`; `tool_call` carries `toolCallId`, `title`, `kind` (ACP's: `read`,
`edit`, `execute`, `search`, `fetch`, `think`, `other`, ...), `status` and `rawInput`; `tool_call_update` carries
`toolCallId`, `status` (`completed` or `failed`), and the tool's output as `content`
`[{"type": "content", "content": {"type": "text", "text": ...}}]`; `plan` carries `entries` of `content`, `status` and
`priority` (Claude Code's todo list, Codex's plan, opencode's todos); `usage_update` carries the runtime's `usage`, and
`cost` `{"amount", "currency": "USD"}` when the runtime counts one. An event a runtime sends that has none of these
meanings goes as `output` `{"raw": ...}`, except the ones that only repeat others: text deltas, the start of a hook,
Claude Code's list of slash commands.

A runtime is available when its binary answers `--version` with at least the version the adapter was checked with,
and the package the adapter needs is installed at least at the version checked: Claude Code 2.1.289 with
claude-agent-sdk 0.2.163, codex-cli 0.153.4 as the app-server with openai-codex 0.160.0 (whose bundled codex is not
used), opencode 1.18.34 with aiohttp. Otherwise the heartbeat reports it unavailable with the reason, and the hub
hands it no run.

### A run on the machine

1. The daemon fetches `origin` in the checkout and makes the worktree `~/.evo/worker/worktrees/<project>-<run>` on
   the plan's branch for the repo, from `origin/<branch>` when the remote has it, else the local branch, else the
   remote's default branch. When that branch is checked out in another worktree (the owner's checkout, say), or has
   local commits the start lacks, the worktree is on `evo-run/<run>` instead and the push still goes to the plan's
   branch. A plan that names no branch for the repo, or names its default branch (the remote's HEAD, the hub's
   `default_branch`, `main` or `master`), fails the run before the agent starts.
2. The run's adapter starts the agent on the prompt in the worktree, and the daemon reports `running` with the
   session id. The owner's messages are handed to the agent when the heartbeat counts them, then acknowledged. A
   cancel, the run's timeout counted from the agent's start, or a heartbeat answering `held: false` interrupt the
   agent; a cancelled run is reported `cancelled`, a timed-out one `failed`, and a run no longer held gets no report.
   A takeover hands the agent to a person and a handback gives it back (see [Interactive runs](#interactive-runs));
   one the worker cannot do (no tmux, or an adapter without a terminal UI) is noted once in the run's log, and the
   run goes on headless.
3. The agent ends its turn and has written `.evo-run/result.json`. The daemon reports `verifying` and runs each of
   its `verify_commands` (1 to 50 shell commands) in the worktree with `/bin/sh`, within the time the run has left,
   and records each exit code and the end of its output as a `system` event. A missing or malformed result file, or
   a command that exits other than 0, fails the run; nothing is pushed and the work stays in the worktree.
4. The daemon commits what the agent left uncommitted as `run #N: <title>`, without `.evo-run/`; refuses to push a
   detached HEAD, a branch the agent switched to, or a default branch; and pushes `HEAD` to the plan's branch on
   origin, never forced and never merged. A push the remote refuses (not a fast-forward, say) fails the run.
5. Once every event of the run is acknowledged, it reports `done` (approval `auto`) or `review`, with the commit,
   the diffstat, the verify results, the agent's summary and its usage. It then uploads the run's log and diff
   when the hub has a blob store, and moves the worktree off the plan's branch, so the owner can check the branch
   out elsewhere. The worktree, and an `evo-run/<run>` branch, are removed 7 days after the run ended; the daemon
   looks every hour.

### Interactive runs

A person can drive a run's agent in its runtime's own terminal UI, in tmux on the worker. Interactive mode needs
`tmux` on the daemon's PATH; without it the daemon says so when it starts, an interactive run fails before its agent
starts, and a takeover is noted as unsupported while the run goes on headless.

- **Takeover.** When the heartbeat says `takeover`, the adapter lets the agent finish its turn
  (`stop_at_turn_boundary`); the daemon then starts the runtime's UI on the same session in the detached tmux session
  `evo-run-N` and reports `interactive` with the session id. The lease goes on being extended. A run dispatched in
  interactive mode, or taken over before its agent started, starts there, the UI given the run's prompt.
- **The UIs.** Claude Code: `claude --resume ID --dangerously-skip-permissions --remote-control evo-run-N`
  (`--session-id` and the prompt for a new session), so the session also shows in the Claude apps through Remote
  Control. Claude Code stops at its folder trust dialog in every new folder, the bypass flag notwithstanding, so the
  daemon first sets `projects[<worktree>].hasTrustDialogAccepted` to true in Claude Code's state file (`~/.claude.json`,
  or `.claude.json` under `CLAUDE_CONFIG_DIR`), for the worktree's path as given and resolved; the file is replaced
  atomically with its mode and every other key, and one that is not a JSON object is left alone (the dialog then
  waits in the terminal). opencode: `opencode attach URL --session ID --dir WORKTREE` on an `opencode serve` of the
  UI's own (127.0.0.1, a free port, a random password), which also creates a new session and hands it the prompt.
  Codex: `codex resume ID --dangerously-bypass-approvals-and-sandbox -C WORKTREE` (`codex` with the prompt for a new
  session), with `--dangerously-bypass-hook-trust` when the help of that codex lists it, so the UI does not stop at
  "Hooks need review"; that flag runs the owner's enabled hooks without their recorded trust. The run's model and
  effort go along when the run names them.
- **The pane.** tmux runs the UI through a script in the run's directory (mode 0700, removed once read) that enters
  the worktree, sets the agent's environment over the tmux server's and executes the UI, so the UI has the run's
  variables whatever server it lands on: the default one, the one of `$TMUX` when the daemon runs inside tmux, or
  `EVO_WORKER_TMUX_SOCKET`. `evo-agents worker attach N` reads the server from the run's record.
- **The log.** While a person drives the agent, the run's log follows the runtime's own record of the session, in the
  same event kinds: Claude Code's transcript (`<config>/projects/*/<ID>.jsonl`), Codex's rollout
  (`$CODEX_HOME/sessions/Y/M/D/rollout-*-<ID>.jsonl`, the one whose `session_meta` names the worktree for a new
  session) and opencode's event stream. What the person types shows as `output`. A UI without such a record is logged
  from its terminal (`tmux pipe-pane`), escape sequences removed, as `output` `{"terminal": text}`. Messages of the
  owner wait in the inbox meanwhile and go to the agent once it is headless again.
- **Handback.** When the heartbeat says `handback`, or the person leaves the UI, the daemon closes the tmux session (a
  turn the person started in the UI is cut there), and a new adapter goes on headless in the same session with a
  message saying the session was handed back; the run is reported `running` and goes on to its verify commands as
  any run. A cancel, the run's timeout or the hub letting go of the run close the UI as well.
- **The web terminal.** When the heartbeat says `terminal_open` for an interactive run, the daemon opens the worker's
  end of the terminal (see [Terminal](#terminal)) with aiohttp and joins it to `tmux attach` of the run's session,
  on a PTY of its own whose size follows the browser's. The last 256 KiB that terminal printed are kept for the run
  and sent first when a browser connects again. A worker whose `config.json` has `allow_web_terminal` off connects
  only to print that in the terminal, then closes: nothing is attached and nothing typed reaches the machine.
- A daemon that starts closes the tmux sessions of the runs a previous daemon left unfinished.

### When the hub does not answer, and stopping

Each event goes to the spool on disk before it is sent; batches of up to 500 events and under 1 MiB go from
`ack_seq + 1`, and the hub's `ack_seq` drops them from the spool. A body over 64 KiB is cut before it is spooled, as
the hub would cut it. The spools of all runs share 256 MiB: an event beyond that is dropped without taking a seq, and
the run's next event that fits is preceded by a `system` event saying how many were dropped. A call that gets no
answer, a 5xx or a 429 is sent again after 1 second, doubling up to 60 (with `Retry-After` when the hub sends one),
while the run goes on. A daemon that starts finds the spools a previous one left and sends them, and marks the runs
it left unfinished as ended.

SIGTERM (or SIGINT) stops the claims, including one waiting; the runs held go on, with heartbeats, until they end or
reach their timeout, and the daemon exits 0. A second signal interrupts the agents and fails their runs. A 401 or
403 (the worker or its token was revoked) stops the daemon with exit status 3, and so does `evo-agents worker run` on
a machine that is not a worker, such as one `evo-agents worker revoke` left: starting the daemon again changes
nothing, so a service does not (see [In the background](#in-the-background)). `EVO_WORKER_REVOKED_EXIT` names
another status for both. A 426 (a protocol the hub no longer speaks) stops the daemon with exit status 1, which an
upgrade of evo-agents mends.

### In the background

`evo-agents worker service install` keeps `evo-agents worker run --quiet` running, naming the evo-agents that ran
the install by absolute path. On macOS it is the LaunchAgent `io.github.maycuatroi1.evo-agents.worker` in
`~/Library/LaunchAgents`, loaded into `gui/<uid>` with `launchctl bootstrap`; on Linux the systemd user unit
`evo-agents-worker.service` in `~/.config/systemd/user`, enabled and started with `systemctl --user`. Elsewhere the
command says so, and `evo-agents worker run` can go under a supervisor of your own.

- The daemon starts at once and at each login. One that exits with an error is started again 10 seconds later
  (launchd `KeepAlive` with `SuccessfulExit` false, systemd `Restart=on-failure`); one that stopped cleanly, exit 0
  after SIGTERM, stays stopped, and so does one that is no longer a worker: the systemd unit lists its exit status 3
  in `RestartPreventExitStatus`, and since launchd tells exits apart only as 0 or not, the LaunchAgent sets
  `EVO_WORKER_REVOKED_EXIT=0`.
- A service manager starts a job with almost no environment, so the service keeps PATH as it was at the install,
  without relative entries and with `/usr/local/bin`, `/usr/bin`, `/bin`, `/usr/sbin` and `/sbin` added when
  missing, and pins `EVO_WORKER_HOME` to the state directory. The install prints where it found `claude`,
  `opencode`, `codex`, `tmux` and `git` on that PATH; install again after one of them moves.
- Stopping the service, by `uninstall` or at logout, sends the daemon SIGTERM and kills what is left 60 seconds later
  (launchd `ExitTimeOut`, systemd `KillMode=mixed` and `TimeoutStopSec=60`).
- The daemon writes `worker.log` itself. What it prints before that log is open goes to `service.log` in the state
  directory on macOS, and to `journalctl --user -u evo-agents-worker` on Linux.
- systemd stops a user's services when the user logs out; `loginctl enable-linger` keeps them running, and the
  install says so when lingering is off.

The install refuses a machine that is not a worker yet, a Python without the worker extra, and a machine where a
daemon already runs outside the service. It waits up to 10 seconds for the daemon to run and fails, leaving the
service installed, when the daemon is not running then. Installing again replaces the service in place.
`uninstall` leaves the token, the configuration and the runs alone. A worker revoked on the web stops at its next
heartbeat, and a daemon started after `evo-agents worker revoke` deleted the token here stops at once. The service
does not start either again; at each login it starts the daemon once more, which stops the same way, until the
machine is a worker again. `evo-agents worker revoke` leaves the service installed and says so; `evo-agents worker
service uninstall` removes it.

## What version 1 does not do

- The terminal relay and the listener behind the event stream live in one api process. That is how the hub runs
  today; more processes or replicas would need a relay through Postgres or a broker.
- Workers run on macOS and Linux; Windows only through WSL. Interactive mode and the terminal need tmux on the
  worker, and a worker without it takes headless runs only.
- A handback closes the terminal UI where it stands: the daemon cannot see a turn the person started there end.
- Only the owner dispatches to a worker; workers shared by a team are a later decision.
- A run of one step never dispatches the next one; a plan run does a plan's steps in one session on one worker,
  never several workers at once. Nothing opens a pull request.
