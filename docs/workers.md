# Workers and runs

A worker is a member's own laptop or desktop, registered with the hub, that runs plan steps for that member. The
member dispatches a ready step; the hub queues a run; the member's worker claims it, runs the step with Claude Code,
opencode or Codex CLI, headless or with a person at the keyboard, and sends logs, state and evidence back while it
works. The hub then records the step's progress in the plan. The worker always calls the hub over HTTPS and never
opens a port on the machine.

This page describes version 1 of the worker protocol as planned. `evo_agents/hub/runs.py` holds the model it rests
on: the run states and who may change them, a worker's status, the event kinds, which steps are ready, and the
prompt a run gives its agent. It needs only the standard library, so the api and the daemon share it. Schema 0009
holds the tables, and `evo_agents/hub/server/workers.py` the routes that register and stop workers (pairings, join,
direct registration, list, drain, undrain, revoke). `evo_agents/hub/server/runs.py` holds the queue: ready steps,
dispatch, claim, heartbeat, state reports, and the owner's cancel, approve and rerun; `evo_agents/hub/server/run_state.py`
moves runs, records each move in the plan, and holds the reaper and the pruning of events. Events, the event stream,
messages, takeover, the terminal and the `evo-agents worker` daemon come in later releases, and this page changes
with them. The daemon has its own command group because `evo-agents hub worker` is
already the server's job worker (see `docs/hub.md`).

## Entities

| Entity | What it is | Table |
| --- | --- | --- |
| worker | a machine its owner registered: a name unique per owner, host facts (hostname, OS, arch, daemon version), 1 to 8 slots, labels, the runtimes and checkouts it reports, whether it allows the web terminal | `workers` |
| worker project | a project the worker may take runs of, chosen at registration | `worker_projects` |
| pairing | a one-time code the web creates so a machine can join without a machine token | `worker_pairings` |
| run | one attempt at one plan step on one worker | `runs` |
| run event | one entry of a run's log, numbered within the run | `run_events` |
| inbox message | a message from the owner to the run's agent, waiting for the worker | `run_inbox` |

A run records the project, plan and step key, the plan revision it was dispatched from, who dispatched it, the worker
(or the worker it is pinned to), the runtime the dispatch asked for (`requested_runtime`: `claude-code`, `opencode`,
`codex` or `any`, which the next attempt asks for again) and the one the run has (`runtime`: the same, except that
`any` becomes the runtime the claiming worker picked), the
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
  agent can read and change anything its owner can, and it runs on the owner's runtime accounts and quotas. The
  daemon refuses to push the default branch or a detached HEAD and never merges.
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
session leaves an audit row (`run.*`, `worker.*`, `terminal.open`, `terminal.close`).

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
| `review` | verified; waits for the owner to approve |
| `done` | approved, or verified with approval `auto` |
| `failed` | the agent, the checkout or a verify command failed, the run timed out, or the last attempt's lease ran out |
| `lost` | its worker stopped extending the lease, and another attempt was queued |
| `cancelled` | stopped on the owner's request |

The worker holds a run, and extends its lease, while it is `leased`, `running`, `interactive` or `verifying`. A run
in any state before `done` is active, and a step has at most one active run. `done`, `failed`, `lost` and `cancelled`
are final: running the step again (rerun, or the next attempt) creates a new run.

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
| `review` | `done` (approve), `cancelled` | owner |
| `leased`, `running`, `interactive`, `verifying` | `failed`, `cancelled` | worker or reaper |
| `leased`, `running`, `interactive`, `verifying` | `lost` | reaper |

A claim and each heartbeat set a held run's lease to expire 300 seconds later. Every minute the reaper
(`hub.recover_runs`) looks for held runs whose lease has expired. Such a run becomes `lost`, and the hub queues a
new run for the same step with `parent_run_id` pointing back and the attempt one higher; a third attempt becomes
`failed` instead, and a run the owner had asked to cancel becomes `cancelled`. Revoking a worker does the same at
once to the runs it holds; a held run pinned to that worker fails rather than coming back, and so does a queued run
pinned to it, since no other worker may claim either. Each of these moves writes a `state` event with the actor
`reaper`. Cancelling a held run sets `cancel_requested_at`; the next heartbeat tells the worker, which stops the agent
and reports `cancelled`.

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
out and that the plan on the hub has the full text.

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
neither draining nor revoked. The oldest such run is leased for 300 seconds. The answer is `{"run": {...}}` with the
id, project, plan, step key and title, plan revision, attempt, max attempts, parent run, runtime, mode, approval,
timeout in minutes, repo, branch, lease expiry and the prompt (built from the plan revision the run was dispatched
from), or `{"run": null}` when the wait ends empty, and the daemon claims again at once. A worker has at most one
claim waiting: a newer claim ends the older one, which answers no run.

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

The hub records the heartbeat, extends the lease of each run named that the worker still holds by 300 seconds, and
answers with control:

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
`cancel: true`. Takeover, handback and the terminal stay false until the routes that ask for them exist.

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
and at least one verify result, every one with exit code 0; `review` needs approval `review`; otherwise 409. A
`failed` report without an `error` gets one naming the worker. Reporting the state the run is in already moves
nothing and keeps the session id, commit, diffstat, verify results and usage it carries, so a resend after a lost
answer is safe. The answer is the run as the hub holds it.

### Events

`POST /v1/worker/runs/{id}/events` sends a batch of at most 500 events and 1 MiB. Each event has `seq`, `at`, `kind`
and `body`. `seq` is the worker's own count for the run: 1 for the first event and one more for each next. The hub
remembers the highest `seq` it has stored with none missing below it and answers with it as `ack_seq`; the daemon
then deletes its spool up to there. An event at or below `ack_seq` is a resend and is skipped, so a batch can always
be sent again safely; one that comes after a gap is not stored, and the daemon sends again from `ack_seq + 1`.

The hub numbers what it stores itself: `run_events.seq` counts all of the run's events, those from the worker and
those the hub writes (`user_message` when the owner sends a message, `state` on each move), and is what
`events?after=SEQ` and the SSE stream's `Last-Event-ID` refer to. A body over 64 KiB is cut and the event marked
`truncated`. A run keeps at most 20,000 events; a batch beyond that gets 413. The full log and the diff go to the blob
store when the run ends, as blob kinds `run-log` (64 MiB) and `run-diff` (8 MiB).

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
| `POST /v1/worker/runs/{id}/events` | sends a batch of events |
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
| `GET /v1/projects/{p}/runs`, `GET .../runs/{id}` | reader | list and show runs |
| `GET .../runs/{id}/events?after=SEQ` | reader | events after a number |
| `GET .../runs/{id}/stream` | reader | the same as server-sent events, with a ping every 15 s, resumed by `Last-Event-ID` |
| `GET .../runs/{id}/diff` | reader | a presigned URL of the run's diff |
| `POST .../runs/{id}/messages` | owner | a message for the agent, at most 8 KiB |
| `POST .../runs/{id}/takeover`, `/handback` | owner | switch between headless and interactive |
| `POST .../runs/{id}/cancel`, `/approve`, `/rerun` | owner | stop, approve a run in review, run the step again |
| websocket `/v1/projects/{p}/runs/{id}/terminal` | owner of the worker | the browser's end of the terminal |

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

The web terminal joins two websockets in the api's memory: the browser's and the worker's. The browser's end checks
the session cookie itself (the HTTP middleware does not see websockets), requires an `Origin` equal to the hub's
public URL, a CSRF token in the first message and a web session created within the last 12 hours; the caller must
own the worker, and the worker must allow the web terminal. The worker's end needs the token of the worker that
holds the run. Opening the terminal on a headless run goes through takeover first.

Frames are binary, at most 64 KiB, and their first byte is their type, as in ttyd: 0 input from the browser, 1 output
from the worker, 2 resize with the new columns and rows. A missing session closes the socket with 4401, anything not
allowed with 4403. A session closes after 15 idle minutes and after 4 hours at most, and a run has one browser at a
time. On the worker, the daemon attaches a PTY to the run's tmux session and keeps the last 256 KiB of output, which
it replays when the browser connects again.

## Limits

| What | Limit |
| --- | --- |
| claim wait | 25 s, looking again every 5 s besides the notifications |
| steps per dispatch | 50 |
| heartbeat | every 15 s |
| offline after | 300 s without a heartbeat |
| lease | 300 s, extended by each heartbeat |
| attempts per dispatch | 3 |
| slots per worker | 1 to 8 |
| run timeout | 5 to 240 minutes |
| pairing code | 10 minutes, 5 unused per member (locked ones included until they expire), locked after 5 wrong tries |
| refused joins | 10 per client address in 10 minutes, then 429 |
| prompt | 32 KiB |
| event batch | 500 events and 1 MiB |
| event body | 64 KiB, longer ones cut and marked `truncated` |
| events per run | 20,000 |
| message to the agent | 8 KiB |
| run log, diff | 64 MiB, 8 MiB in the blob store |
| events kept | 30 days after the run ends (`EVO_HUB_RUN_LOG_DAYS`) |
| terminal | 64 KiB frames, 15 minutes idle, 4 hours, one browser per run, sessions under 12 hours old |
| daemon spool | 256 MiB |
| daemon worktrees | removed 7 days after their run ends |

## What version 1 does not do

- The terminal relay and the listener behind the event stream live in one api process. That is how the hub runs
  today; more processes or replicas would need a relay through Postgres or a broker.
- Workers run on macOS and Linux; Windows only through WSL. Interactive mode and the terminal need tmux on the
  worker, and a worker without it takes headless runs only.
- Only the owner dispatches to a worker; workers shared by a team are a later decision.
- A finished step never dispatches the next one, and nothing opens or merges a pull request.
