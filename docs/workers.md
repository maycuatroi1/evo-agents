# Workers and runs

A worker is a member's own laptop or desktop, registered with the hub, that runs plan steps for that member. The
member dispatches a ready step; the hub queues a run; the member's worker claims it, runs the step with Claude Code,
opencode or Codex CLI, headless or with a person at the keyboard, and sends logs, state and evidence back while it
works. The hub then records the step's progress in the plan. The worker always calls the hub over HTTPS and never
opens a port on the machine.

This page describes version 1 of the worker protocol as planned. `evo_agents/hub/runs.py` holds the model it rests
on: the run states and who may change them, a worker's status, the event kinds, which steps are ready, and the
prompt a run gives its agent. It needs only the standard library, so the api and the daemon share it. The tables,
the routes and the `evo-agents worker` daemon come in later releases, and this page changes with them. The daemon
has its own command group because `evo-agents hub worker` is already the server's job worker (see `docs/hub.md`).

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
(or the worker it is pinned to), the runtime (`claude-code`, `opencode` or `codex`; a dispatch may ask for any), the
mode (`headless` or `interactive`), the approval (`auto` or `review`), a timeout of 5 to 240 minutes, its attempt out
of at most 3, the run it retries (`parent_run_id`), its state and lease, the agent's session id, the repo and branch,
and at the end the commit, diffstat, verify results, evidence, usage and error.

## Who may do what

- **A worker belongs to the member who registered it, and only that member dispatches to it.** A claim takes only
  runs whose dispatcher owns the worker, and a dispatch that names a worker must name one of the caller's own (403
  otherwise). Another writer of the same project never gets a run onto your worker. A hub admin sees every worker
  and may drain, undrain or revoke one, but cannot dispatch to it.
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
  nothing beyond the runs it holds. Revoking the worker ends the token at once.

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
once to the runs it holds. Cancelling a held run sets `cancel_requested_at`; the next heartbeat tells the worker,
which stops the agent and reports `cancelled`.

The hub writes the plan as the member who dispatched the run, through the same item update as `evo harness step`,
with `if_revision`, retrying up to 5 times on a revision conflict. When a run starts `running`, the step becomes
`in_progress` with the note `run #N on worker W`. When it ends `done`, the step gets `done`, `done_at` and
`evidence` (commit, verify commands, results) in one revision. When the last run of a dispatch ends `failed`,
`cancelled` or `lost`, the step goes back to `pending` with a note naming why.

## Which steps are ready

A step is ready when its status is `pending` and every id in its `depends_on` names a step whose status is `done`
(`ready_steps` in `runs.py`). Ids compare as text, so `5` and `"5"` name the same step. A step without a status
counts as pending. A step whose status is `blocked`, `in_progress` or `done` is not ready, and neither is a step that
depends on a blocked step or on an id the plan does not have. `unready_reason` says why a step is not ready, in words
the web can show next to it.

The `blocking` flag is not read. It is execute-plan's gate for a session that works through a whole frontier: a
blocking step that is not done holds back every step after it. A run is one step its owner chose to dispatch, and
in a plan where every step is blocking, honouring the flag would hold back steps that `depends_on` lets run.

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

Every request to `/v1/worker/*` carries `X-Evo-Worker-Protocol: 1`. A request with another value, or without the
header, gets 426 in the hub's usual error shape, so an old daemon learns that it must be upgraded instead of
misreading answers. Routes for members do not use the header.

### Joining

The owner creates a pairing on the web (`POST /v1/workers/pairings`), naming the worker, its projects (each one the
owner holds writer on), slots, labels and whether it allows the web terminal. The code is 8 characters of Crockford
base32 written `XXXX-XXXX`; it lasts 10 minutes, a member has at most 5 unused codes, and the hub keeps only its hash.
The web polls `GET /v1/workers/pairings/{id}` until the machine joins. On the machine, the daemon sends the code and
its host facts to `POST /v1/worker/join`, the one public worker route, and gets its `evw_` token once. Five wrong
tries lock the code. A machine already signed in with `evo-agents hub login` can register directly instead
(`POST /v1/workers` with its machine token).

### Claim

`POST /v1/worker/claim` waits up to 25 seconds for a run. The hub listens on the Postgres channel `evo_runs`, which
each dispatch notifies, and also looks again periodically, so a missed notification delays a claim but never loses
one. It picks a queued run with `SELECT ... FOR UPDATE SKIP LOCKED` when all of these hold: the worker's owner
dispatched it, the project is one of the worker's and the owner still holds writer on it, the worker has the runtime
and a checkout of the repo and a free slot, and it is neither draining nor revoked. The answer is the run (id,
project, plan, step, attempt, runtime, mode, approval, timeout, repo, branch, lease expiry and the prompt), or no run
when the wait ends empty, and the daemon claims again at once. A worker has at most one claim waiting.

### Heartbeat

Every 15 seconds the daemon sends `POST /v1/worker/heartbeat` with its runtimes, checkouts, free slots and the runs
it holds. The hub records the heartbeat, extends the lease of each of those runs by 300 seconds, and answers with
control for the worker: for each run, whether to `cancel`, `takeover`, `handback` or open the terminal
(`terminal_open`) and whether the `inbox` has new messages; for the worker, whether to `drain`. A run the worker
reports but no longer holds (lost, cancelled, or another worker's now) comes back with `cancel`.

### State

`POST /v1/worker/runs/{id}/state` reports a move of a run the worker holds, with the commit, diffstat, verify results
and usage when it has them. The hub checks the move against the transition table with the worker as actor and answers
409 when the table refuses it, and 404 when the worker does not hold the run.

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
| `POST /v1/workers/{id}/drain`, `/undrain`, `/revoke` | owner or hub admin | stop new claims, resume them, end the worker |
| `GET /v1/projects/{p}/plans/{plan}/ready-steps` | reader | the steps that may be dispatched |
| `POST /v1/projects/{p}/runs` | writer | dispatch steps |
| `GET /v1/projects/{p}/runs`, `GET .../runs/{id}` | reader | list and show runs |
| `GET .../runs/{id}/events?after=SEQ` | reader | events after a number |
| `GET .../runs/{id}/stream` | reader | the same as server-sent events, with a ping every 15 s, resumed by `Last-Event-ID` |
| `GET .../runs/{id}/diff` | reader | a presigned URL of the run's diff |
| `POST .../runs/{id}/messages` | owner | a message for the agent, at most 8 KiB |
| `POST .../runs/{id}/takeover`, `/handback` | owner | switch between headless and interactive |
| `POST .../runs/{id}/cancel`, `/approve`, `/rerun` | owner | stop, approve a run in review, run the step again |
| websocket `/v1/projects/{p}/runs/{id}/terminal` | owner of the worker | the browser's end of the terminal |

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
| claim wait | 25 s |
| heartbeat | every 15 s |
| offline after | 300 s without a heartbeat |
| lease | 300 s, extended by each heartbeat |
| attempts per dispatch | 3 |
| slots per worker | 1 to 8 |
| run timeout | 5 to 240 minutes |
| pairing code | 10 minutes, 5 unused per member, locked after 5 wrong tries |
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
