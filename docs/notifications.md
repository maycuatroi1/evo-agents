# Notifications and decisions

A plan run (see [Plan runs](workers.md#plan-runs)) works for hours without anyone watching. Two things in it need
the member who dispatched it, the run's owner: a decision the agent may not take alone, and a push or merge the
owner should know about. The hub turns each into a notification for the owner and hands it to every channel the
owner has turned on. The web was the one channel of 0.4.0; Telegram, at the end of this page, came later as a class
added to the channel registry, with routes of its own and no change to the outbox. Push to a member's browsers and
phones (Push to devices, below) came with plan hub-mobile-phase-0, in the hub's own code rather than a plugin: each
notification has a tier for its member, and only the interrupt tier is pushed, outside the member's quiet hours.

`evo_agents/hub/runs.py` holds the lists below (decision categories, states and limits, notice kinds, delivery
states), with the standard library only, so the api and the worker daemon share them. Plan runs, decisions and
notifications are new in 0.4.0: schema 0010 holds their tables, and the routes, the daemon's commands and the web's
Inbox come in the same release.

## Terms

| Term | What it is | Table |
| --- | --- | --- |
| decision | a question the agent of a plan run, an author run or a chat run asks its owner, with 2 to 6 options, open until it is answered, expires or is cancelled | `decisions` |
| notice | something the owner should know and need not answer, such as a push to a default branch | none of its own: it is a notification of kind `notice` |
| notification | one decision or notice for one member, read or not | `notifications` |
| channel | a place where a member gets notifications: the web, later Telegram | `notification_channels` |
| delivery | one notification on one channel or one push device, with its attempts | `notification_deliveries` |
| registry | `CHANNELS` in the api: for each channel kind and each kind of push device, the class that sends a notification there | code, not a table |
| push device | a browser's Web Push subscription, an iPhone's APNs token or an Android phone's FCM token, of one member | `push_devices` |
| tier | how a kind of notification reaches a member: `interrupt` (pushed to their devices), `brief` or `off` (not pushed) | `notification_preferences.tiers` |
| quiet hours | the hours of a member's day in which an interrupt waits to be pushed until they end | `notification_preferences` |
| Today | what waits for a member and what changed since they last looked, worked out at each view, and its summary push | `today_marks` |

## Decisions

The agent of a plan run asks with `evo-agents worker ask`, and only about these categories (`DECISION_CATEGORIES`). The
prompt of a run with a snapshot of autonomy opens with its autonomy card (`autonomy_card.card`, plan autonomy-levels):
the principal, their role and unit, the person an intern reports to, the effective level, the classes in deny, in ask
with the category to ask each in (`CLASS_CATEGORIES`: K5 `merge`, K6 `deploy`, K8 `credential`, K9 `delete_data` and
`live_migration`, K10 `external_send`, K11 `spend_money`, K12 `access`), and in notify; at A5 only the floors S2 and S3.
A plan run's prompt then lists the categories its card leaves to ask, with `architecture` and `scope`, and says that
merging, deploying and releasing are the agent's only when K5 and K6 are auto. A run without a snapshot (a local run,
one queued before schema 0113) lists all of them but `deploy` and the three of schema 0115
(`PROMPTED_DECISION_CATEGORIES`), as before. The route that takes a decision refuses any other category, and the agent
decides everything else itself and writes each such choice, with why, into the evidence of its step on a line that
starts with `Decision:`.

A decision goes to one person, `asked_of` (schema 0115, `principals.asked_of`): for a run whose principal is an intern,
the person their seat reports to, and once it has been open 24 hours the leads of their unit too (the hourly job
hub.expire_seats tells them, once each, and each may then answer); for a run the hub makes on its own, the sponsor of
its agent; for any other, the run's principal. An intern never answers a decision of their own run (403). The Inbox,
the decision board, `hub decision list` and `hub decision show` name that person, and the notification's body, which
Telegram sends as it is, ends with "asked of <login>". A notice of a class in notify reaches the principal, and the
person they report to when they are an intern.

The hub asks decisions of its own too. A gate of autonomy whose class is in ask, before a merge (K5), a deploy or a
release (K6), opens one of the category `autonomy` (schema 0114, `runs.AUTONOMY_DECISION_CATEGORY`) to the run's
principal, and acts once they approve; only that principal answers it (`docs/autonomy.md`).

| Category | What it covers |
| --- | --- |
| `deploy` | deploying or releasing anything to any environment, a checkpoint step that deploys included |
| `delete_data` | deleting data that is not the run's own scratch: database rows, files, buckets, other people's branches |
| `live_migration` | a migration, backfill or bulk change on real data rather than a test database |
| `external_send` | sending anything to a service outside the machine and the repos' own remotes: mail, chat, issues, third-party APIs |
| `spend_money` | anything that costs money beyond the runtime's own usage: paid APIs, cloud resources, purchases |
| `architecture` | an architectural choice the plan leaves open |
| `scope` | a question of scope the plan leaves open: adding, dropping or reshaping a step or what it delivers |
| `merge` | merging into a repo's default branch or the target branch of a pull or merge request (K5) |
| `credential` | creating, rotating or revoking a credential: a token, a key, a secret, an account's password (K8) |
| `access` | changing grants, autonomy ceilings or preferences, policies or agent profiles (K12) |

A decision holds the run, its project, plan and step (the step is optional), the category, a question, a context in
markdown of at most 16 KiB, and 2 to 6 options. An option has a key (letters, digits, `_` and `-`, at most 32
characters, starting with a letter or digit), a label, an optional description, and whether it is the one the agent
recommends; at most one is. The owner answers with an option, with text of their own, or with both.

### Why the agent recommends, how to undo, what it rests on

So that the person asked can answer on a phone in one tap, a decision may also say why the agent recommends its
option, whether the act it asks about can be undone and how, and what the question rests on (schema 0148,
`evo_agents/hub/decision_asks.py`). Each field is optional, and a decision without them reads and answers as before.

| Field | What it holds |
| --- | --- |
| `recommended_reason` | why the recommended option, in a sentence or two, at most 500 characters; only with `recommended` (422 otherwise) |
| `reversible` | for an act: `true` or `false`; `null` when the question asks about no act, or nobody said |
| `undo` | how to undo the act, at most 500 characters; only with `reversible` true (422 otherwise, and a CHECK of the table) |
| `evidence` | at most 20 refs: `run:ID`, `run:ID:SEQ`, `output:ID`, `P-<n>/E-<n>` (a line of evidence of a problem; a bare `P-<n>` is refused), an https URL, or `session:ID[:FIELD:INDEX]`, `transcript:ID[:FILE]:LINE`, `code:REPO:PATH[:LINE]` and `memory:ID:REV` as `review.parse_evidence` reads them. A ref of any other form is 422. Only the form is checked, not that the thing exists |

The fields come in through the worker's route (`POST /v1/worker/runs/{id}/decisions`) and `evo-agents worker ask`
(`--recommended-reason TEXT`, `--reversible yes|no`, `--undo TEXT`, `--evidence REF` repeated; `docs/workers.md`), the
MCP tool `ask_member` and a local run's decisions. They go out through the decision routes, `evo-agents hub decision
show` and `hub decision list` (with `--json` too), the MCP tool `decision_list`, which also names the recommended
option, and `extra["decision"]` of a core plugin's channel (`docs/plugins.md`). The API gives evidence back as a list
of refs written the way the command line writes them (`code:r:./a.py` becomes `code:r:a.py`), `[]` when there is none,
and no links: the web makes each ref a link (a run's page, the decision's run with its outputs, a problem, a memory,
`/kg?q=PATH` for code, an https page in a new tab; `session:` and `transcript:` refs stay text). `--undo` without
`--reversible` sends `reversible` true, and `--undo` with `--reversible no` is refused before anything is sent.

The prompts of plan runs, step runs and author runs, and the reminders of autonomy that tell an agent to ask, tell it
to give every question with a natural choice `--recommended` and `--recommended-reason`, an act `--reversible` (with
`--undo` when it can be undone), and `--evidence` for what it rests on (`decision_asks.ASK_RULE`). `evo-agents worker
ask` without `--recommended` prints a warning to stderr and still asks. The hub fills the recommendation of its own
decisions where it has a default answer: a decision to log in recommends `done` (the run asked for the login to go
on), a stopped release cycle `close` (it spends no more of the cap, and the schedule's next daily check starts a new
cycle if needed), a work item of the Loop that failed three times `manual`, each with its reason. A wait and a gate of
autonomy carry none: the hub answers a wait itself, and the principal of a gate chose to be asked about an act whose
risk the hub cannot judge.

An option may also say not yet (`waits`, which `evo-agents worker ask --wait-option KEY` sets): choosing it leaves
the decision `open` with `held_at` set and its notification read, sends nothing to the agent, and the run goes on
waiting, or parked, for a later answer with another option. Words with such an option are 422, since no agent would
read them; at least one option of a decision does not wait. The audit row `decision.answer` ends `held`, and the web
says "you said not yet" on the open card.

### Its life, and the run's

| Decision | Run | What happened |
| --- | --- | --- |
| `open` | `running` | the agent asked and goes on with work that does not depend on the answer |
| `open` | `waiting` | the agent's turn ended with the decision still open; the worker keeps the run, its slot and its lease |
| `answered` | `running` | the owner answered; the answer reached the agent in the same session |
| `open` | `parked` | nobody answered for 24 hours (`DECISION_WAIT_SECONDS`); the worker stopped the agent at the end of its turn, freed the slot, and kept the session and the worktrees |
| `answered` | `done`, and a new run `queued` | the owner answered a parked run: the hub queues a plan run pinned to the same worker that resumes the same session in the same worktrees (`resume_of_run_id` names the parked run), and the parked run is done with the note `resumed as #N` |
| `expired` | `cancelled` | the run stayed parked for 7 days (`PARKED_DAYS`) |
| `cancelled` | `cancelled`, `failed` or `lost` | the run ended another way; a decision without its run cannot be answered |

Waiting and parked do not count toward the run's timeout: the timeout of a plan run, 2, 4, 8 or 24 hours, counts the
time the agent runs (`run_seconds` in `runs.py`).

### A decision of an author run

The agent of an author run (`docs/workers.md`, "The chat of an author run") asks decisions too, on a worker whose
daemon lists `author_decisions` in its heartbeat's `features`: its prompt names `scope` for the scope, outcome,
compatibility and rollout of the plan and `architecture` for a choice of design, and the route takes any category of
the table above with the bounds of a plan run's. The decision keeps the plan the run has when its agent asks; one the
agent asks before its run put a plan has none (`plan_id` null), and its readers are the readers of the run. Its
notification, kind `decision`, goes to the run's owner with a button per option on the web and on Telegram like a
plan run's, and when it has no plan the message names `Author run #N` and the run's title in place of the plan.
`evo-agents hub decision list` and `decision show`, and the MCP tool `decision_list`, show such a decision as `author
run #N`, never `None` or `null`.

The life differs from a plan run's in when the agent wakes. A turn that ends with decisions it asked still open waits
(`waiting`) until every one of them has an answer, the owner replies in the run's chat, or the owner ends the chat; a
single answer while another is open does not wake the agent, and the next turn takes every answer there is. A parked
author run resumes only once none of its decisions is open, or on a reply in its chat, and the answers written while
it was parked reach the agent in the first turn of the run that resumes it. When the run ends (End chat, cancel,
fail), its decisions still open are `cancelled`. The answer goes to the run's inbox but is no message of its chat: on
the web the decision's card in the run's Chat tab shows it.

### A decision of a chat run

The agent of a chat run asks its owner decisions of the one category `triage`, with the bounds of a plan run's
(2 to 6 options), on a worker whose daemon lists `chat_decisions` in its heartbeat's `features`; every other category
is 422 for a chat run, and `triage` is 422 for any other run. A turn that ends with a decision it asked still open
leaves the run `waiting`, which keeps its slot and its lease; a chat run with no decision ends after its turn as
before. The answer goes to the run's inbox and starts the next turn in the same session, once none of the run's
decisions is open or the owner writes in the thread. The reaper parks a waiting chat run after 10 minutes
(`CHAT_DECISION_WAIT_SECONDS`, not the 24 hours of a plan run, so a slot is not held for a day); an answer or a reply
in the thread then queues a chat run pinned to the same worker that goes on in the same session, worktrees and branch
(`resume_of_run_id` names the parked run, which is `done`), with the decisions still open and the unread inbox moved to
it. When the run ends otherwise, its open decisions are `cancelled`. The Inbox, the web and Telegram answer such a
decision through the same route as any other (migration 0045 allows the category).
When the failure is one of the frame's (`docs/workers.md`, "Failure triage"), the agent acts by itself, each act with a
`Decision:` line in the thread; the decision is for what is outside it.

Only the run's owner answers, through the web or with a machine token of their own: another member gets 403,
someone without a grant on the project 404, and the worker's own token 403, so the agent cannot answer itself. A
decision that is no longer open gets 409. The answer goes into the run's inbox as a message that names the decision
id, and the heartbeat hands it to the agent like any message of the owner's (`docs/workers.md`, The inbox). The audit
row `decision.answer` names the decision and the option, never the text.

### A decision of a local run

A local run (`docs/workers.md`, Local runs) asks decisions of category `question`, with options and no plan needed. Only
the member who owns the session sees, is notified of and answers it (404 for anyone else). It has no deadline to park
at (`parks_at` is empty); it is `expired` when the session ends silent and `cancelled` when the session ends. The first
answer wins, from the web, Telegram, `hub decision answer` or the machine's report; a later one gets 409.

The agent of a local session asks in two ways, and both end in the same decision:

- The `ask_member` tool of the hub's MCP (`question`, 2 to 6 `options` with a `key` (default 1, 2, ..), `label` and
  `description`, `recommended` as a key or a label, `context`, `step`): the run turns `waiting`, the member is told on
  the web, Telegram and the Inbox, and the call holds up to 20 s (`ASK_WAIT_SECONDS`, inside the proxy's 120 s) for the
  first answer, which comes back with the words the member wrote. With none it says so and the agent calls it again
  with `decision_id` to wait longer, or goes on. A session that is not a hub project's, or has `EVO_HUB_LOCAL_SYNC=0`,
  gets a refusal and uses AskUserQuestion as before.
- AskUserQuestion: the `local-ask` PreToolUse hook puts each question of the dialog (4 at most, options 2 to 6, keyed
  1, 2, ..) to the hub and returns at once, so the dialog opens without delay. An answer given in the dialog is reported
  by PostToolUse (`.../decisions/{id}/answered`) and closes the decision everywhere; a hub answer that came first
  closes the dialog's report with a 409, which the machine leaves alone. This route cannot fill a dialog already open
  (a non-goal: a Desktop dialog is not woken), so the hub's answer reaches the session as below.

An answer that comes after the session stopped waiting stays in the run's inbox; the UserPromptSubmit hook gives it to
the agent as `additionalContext` of the person's next message and acknowledges it with the next one.

### A decision of a release cycle

The hub itself asks decisions of category `release`, never an agent: when a cycle of a project's daily release stops
(`docs/releases.md`, "When a cycle stops"), the owner of the release schedule gets one on the cycle's last run, with
the three options `continue` (one more cap of the schedule), `close` and `pause` (the schedule). Only an admin of the
project answers it, through the Inbox or `hub decision answer`, with one of those options; any other answer is 422,
and one for a cycle no longer stopped 409. The hub does the option and says so in the cycle's thread of the triage
chat. No run waits on the answer.

### A decision of the Loop

The hub itself asks decisions of category `loop` too (plan loop-engine): when a work item of a project's Loop fails a
third time, the person its last run stands for (the run_as of a task run's agent type, the owner of `loop.worker` for
an author or a plan run) gets one on that run, with the three options `retry` (the Loop hands it out again, its
failures counted from 0), `drop` and `manual` (the work item cancelled, its chain left to a person). That person
answers it, through the Inbox or `hub decision answer`, with one of those options; any other answer is 422. The hub
does the option; an answer for a work item that moved on since does nothing. No run waits on the answer.

### A decision to log in

The agent of a run with a browser that meets a login page runs `evo-agents worker browser login` (plan agent-browser,
[workers.md](workers.md#the-browser-pack-and-the-login-decision)), which stores a decision of category `login` on the
run, with the options `done` and `skip` and the link to the live view of the run's browser in its context. The run
waits for its owner, holding its slot and its browser; the Inbox and Telegram carry the decision as any other. Done
saves the browser's profile to the identity and the agent goes on signed in; Skip saves nothing. A second call while one
is open returns the open decision. A run that cannot wait (a step run) gets no decision: its identity is set
`needs_login` and the run fails.

### A wait

The agent of a plan run that needs a time, or something that happens on the hub, waits for it rather than asking its
owner to say when it came: `evo-agents worker wait --reason TEXT --until TIME --on KIND --contains KEY=VALUE --step
KEY` (POST /v1/worker/runs/{id}/waits, `evo_agents.hub.server.run_waits`). It stores a decision of category `wait`
whose question is the reason, whose context says what the run waits for, and whose options are `now` (Go on now) and
`stop` (Stop waiting). It notifies nobody, since it asks nobody anything, and intern escalation leaves it alone; it is
open like any decision, so the run is `waiting` at the end of the agent's turn and the reaper parks it after a day.

`wake_at` is when it wakes at the latest: `--until` (a time with a timezone, or a span such as `+6h`), at most
`MAX_WAIT_DAYS` (7) days ahead, and that far when only `--on` is given. `wake_on` is the event that wakes it sooner: an
event of the run's project in hub_events of kind `--on` (`canvas.claim`, `canvas.baseline`, `signal.posted`,
`srs.version.approved`, `tasks.status`, or a kind a core plugin records of its own) whose payload contains each
`--contains` pair (JSONB containment), written after the wait was asked. On each pass, every minute, the reaper answers
every open wait whose event or time came, before it parks or expires runs: the decision is `answered` by nobody
(`answered_by` null, which only a wait may be; migration 0140), its `answer_text` the message the agent gets, which
names the decision, the event with its payload or the time, and the reason. A parked run is resumed by the reaper, as an
answer resumes it, and a parked run whose wait has not come is not cancelled at `PARKED_DAYS`. The person asked may
answer a wait sooner like any decision.

## Notices

A notice needs no answer (`NOTICE_KINDS`):

| Kind | Sent when | Holds |
| --- | --- | --- |
| `push_default_branch` | a plan run pushed to a repo's default branch, which the plan names for that repo | the repo, the branch, the commits |
| `merge_default_branch` | a plan run merged into such a branch; or the hub's merge queue on GitLab asks each admin of the project after a merge: an audit sample, the approval of a manual PROD release, or which suspect to revert (`details.ask` `sample`, `release` or `revert`; `docs/merge-queue.md`) | the repo, the branch, the commits; for the queue, what it asks and a link to the project's merge queue |
| `plan_finished` | a plan run ended with every step of its plan done | the plan, the steps the run did |
| `run_failed` | a plan, review or judge run failed, or its last attempt was lost; a run of one step or an author run failed for a cause of its preflight (`origin`, `credentials`, `missing_tool`: `docs/workers.md`, "Preflight and failure causes") | the run, the error (and the cause); the triage thread of the run when it opened one (a failed step, plan or author run opens a thread in the project's chat "Triage: PROJECT", or "Triage: PLAN" for a plan whose label is not at or below the project's default label, for the project's default agent to read; `projects.triage_enabled` false opens none) |
| `curator_brief` | the charter's `brief_at` came, in its time zone: the Curator's morning brief to the owner of the night shift's schedule (`docs/hub.md`, "The Curator's review") | the night's runs and cost, its review run, merges, runs waiting for approval, the Curator's changes still planned with why, open decisions and proposals, the worker on duty's last heartbeat |
| `curator_paused` | the circuit breaker paused a project's night shift: the charter's `max_failed_in_a_row` jobs of a night in a row failed or were reverted (`docs/hub.md`, "The Curator's ledger, outcomes and circuit breaker") | the night, the jobs in a row, the runs and the reverted proposals among them, the runs it cancelled |
| `release` | a cycle of a project's daily release published its version: its tag's `release.yml` passed, its job `publish` included (`details.event` `published`), or the hub cannot read the CI of its tag because the GitHub App's installation grants no Actions (`actions_permission`); `docs/releases.md` | the cycle, the tag, the version, the workflow run and the version's page on PyPI; or the problem and how a member grants the permission |
| `merge_paused` | the breaker of a repo of the hub's merge queue on GitLab tripped, to each admin of the project: the queue of that repo waits until an admin resets it (`docs/merge-queue.md`, The breaker) | the repo, its target branch, the cause and its reason, the merge requests it paused, how to reset it, and a link to the project's merge queue |
| `pm_daily` | a working morning's digest of a project that enables the core plugin pm, to its PM (the config's `pm`, else the project's admins), when the project grants the plugin `notifications:send` (`docs/pm-plugin.md`, The morning job) | counts of what was done, late, in review, stale, blocked and to escalate, as the member may see them, the codes of the late, stale and escalated tasks, and a link to the project's pm page; never a title |
| `pm_escalation` | a blocker of an open task that has lasted longer than `escalate_after_days` working days, once a day, to the PM and to whom it waits on (the member, the assignees of the task it waits on, the owner of the run whose decision is open) | the task's code, the kind of blocker, its age in working days and a link to the project's pm page; never the reason or a title |
| `loop_paused` | the breaker of a project's Loop tripped: it made more work items in an hour than `loop.max_items_per_hour`, to each admin of the project; the Loop reads no event and hands out no run until an admin resets it (`evo-agents hub loop resume`) | the work items of the hour, the cap, how to reset it, and a link to the project's Loop page |
| `goal_review` | the first day of a month in the Loop's zone (`loop.timezone`, else the charter's, else UTC), or the first pass of `hub.loop_notices` within its first week: each admin of a project whose Loop is on, and each owner of one of its active goals, once that month (gate 1 of the decision board, `docs/hub.md`, "The decision board") | the active goals with their metric, whether it meets its target, and their deadline; a link to the decision board |
| `plugin_notice` | an isolated plugin sent it through `POST /v1/plugin-api/projects/{project}/notifications` (`docs/plugins.md`, Notifications from a plugin), to the logins and roles it named that hold a grant on the project, when the project granted it `notifications:send`; no rate limit | the title and body the plugin wrote, and in the details the plugin, its version, the topic and the key; a link inside the plugin's pages or the project |
| `loop_brief` | 07:00 in the Loop's zone, within three hours, once a day: the Loop's part of the morning brief, for a project whose Loop is on and whose Curator sends no `curator_brief` (no night shift, or the feature `curator` off), to each owner of one of its active goals | the runs the Loop handed out and the work items it finished in the last 24 hours, and those waiting for a person by reason; a link to the decision board |
| `author_waiting` | an author run's agent ended its turn and the run waits for its owner's reply in its chat (`docs/workers.md`, "The chat of an author run"), with no decision of the run open; the next one reads the run's earlier ones | the agent's last message, the plan it wrote or revises |
| `chat_mention` | someone mentioned the member in a group chat (`docs/hub.md`, "Group chats"), a person or the agent of a run (`@handle`) | the chat, the sender and the message; in a restricted chat only the chat's name and the link |
| `chat_added` | someone added the member to a group chat; the agent of a run that creates a chat (`evo-agents worker chat create`) tells the person who dispatched the run, and each person it adds | the chat and who added them; in a restricted chat only the chat's name and the link |
| `run_outputs` | a run's agent sent files, or left them in its outputs directory (`docs/hub.md`, "Outputs of runs"), to the run's owner, gathered a minute a run (below) | the run, the files' names, sizes and captions; `details.outputs` their ids, and the Inbox lists them as `outputs` |
| `autonomy` | a class in notify acted for a run (the hub's gate, or the worker's hook through `POST /v1/worker/runs/{id}/autonomy-acts`), to the run's principal and to the person they report to when they are an intern; and a stop of autonomy S4 cancelled the member's runs, or was lifted (`docs/autonomy.md`) | the class, the act, the run and its agent; a stop's scope, reason and the runs it cancelled; never a credential |
| `worker_doctor` | the doctor report of a worker shared with units of the org chart has a high finding and the one before had none (`docs/workers.md`, Shared workers), to the worker's owner; the worker takes no run of those units' members until a report has none | the worker, the codes of the high findings, and a link to the worker's page; never a credential |
| `today` | the summary push of Today (Today and its summary push, below): a member with a push device, away from the hub for 8 hours, at their `digest_at` in their time zone, once a local day, when something changed since they last looked | the sentence of Today, which counts things and names none; no project; read when it is stored and delivered to the member's push devices alone, so the Inbox keeps a record and no channel repeats it |

A worker sends only the first four (`runs.WORKER_NOTICE_KINDS`); the hub alone sends `curator_brief`,
`curator_paused`, `release`, `merge_paused`, `loop_paused`, `goal_review`, `loop_brief`, `plugin_notice`, `author_waiting`, `pm_daily`,
`pm_escalation`, `chat_mention`, `chat_added`, `run_outputs`, `worker_doctor` and `today`, and the `merge_default_branch` of its merge queue. A
release run that fails sends no
`run_failed` and opens no triage thread of its own: its cycle stops and asks the owner instead. A chat's notice is
kept in the project of one of its agents, a restricted one first, so a member who loses the grant on it stops getting
it; a chat without agents keeps it in no project. The daemon sends `push_default_branch` itself for the pushes of `evo-agents
worker step`; for a push or merge the agent makes itself, the prompt tells it to run `evo-agents worker notify`. A run of one step never pushes a default
branch and sends no notice.

The hub sends no `author_waiting` when an author run moves to `waiting` with a decision of its own still open: the
decision's notification told the owner already, and Home lists the run in Needs you through its decision, not again
among the author runs waiting for a reply. An author run that waits with no decision open sends `author_waiting` as
before.

### The files of a run

The hub sends `run_outputs` itself (`evo_agents/hub/server/output_notices.py`), in the transaction that records a
run's outputs, to the member who dispatched the run, for the files of source `sent` and `outputs`; an image taken out
of a tool's result (source `tool`) never makes or joins one. It gathers a minute a run (`runs.OUTPUT_NOTICE_SECONDS`):

- the first files store the notification, with every delivery of it due a minute later, so the Inbox shows it at once
  and the channels wait for more;
- the files that come before any channel tried it join it: its `details.outputs`, title (`Run #N sent report.md`,
  `Local run #N sent 3 files`) and body (each file's name, size and caption, 20 at most, then how many more) are
  written again, and it is unread again;
- once a channel took it, or is sending it (its delivery is locked by the job's pass), the next files start a new
  notice, due a minute after they came. So no channel gets two notices of one run within a minute, a delivery that
  failed and is tried again aside.

A file that a question carries goes with the question: once a decision names it in `output_ids`, it leaves the run's
notice no channel has tried yet, and a notice left with no file goes, its deliveries with it. A chat run sends no
`run_outputs`: its files go under its messages in the thread. The Inbox lists the files a notification carries in
`outputs` (`RunOutput`, as `docs/hub.md` describes it): the files of a `run_outputs` still there, with the run's URLs
of their bytes, and the files of a decision, with the decision's.

A member turns `run_outputs` off for themselves (`runs.MUTABLE_NOTICE_KINDS`, schema 0090's `notification_mutes`):
the hub then stores no such notice for them, on any channel, and their decisions and every other notice still come.
`GET /v1/me/notification-settings` answers each kind a member may turn off with whether it is on, and `PUT` with
`{"notices": [{"kind": "run_outputs", "on": false}]}` sets the kinds it names, audited `notification.settings` with
the kinds and no text. The same two routes read and set what a member chose for push (Push to devices, below).

## Notifications, channels and deliveries

Each decision and each notice becomes, in the transaction that stores it, one notification for the run's owner with a
title, a body, a link to the web page that shows it, and the project, run and decision it belongs to; and one
delivery of it for each channel the owner has turned on, and, when it is of the interrupt tier for the owner, one for
each of their push devices (Push to devices, below). The table is the outbox: nothing is sent while the transaction is
open, and a hub that stops between the two loses nothing.

| Table | Columns |
| --- | --- |
| `notifications` | user, kind (`decision`, `notice` or `proposal`), the decision, the notice's kind and details (repo, branch, commits), or the proposal, project, run, title, body, link (a path of the hub's site), `created_at`, `read_at` |
| `notification_channels` | user, kind (not `web`, at most one of a kind per member), `config` (JSON the channel's class reads), `enabled`, `created_at` |
| `notification_deliveries` | notification, channel (none for the web) or push device (`device_id`, schema 0149; never both, by a CHECK), state (`pending`, `delivered`, `failed`), attempts, `next_at`, `last_error`, `delivered_at` |

A notification of kind `proposal` (schema 0014) is a tier 2 proposal of the Curator's review run in its owner's Inbox:
when a review run ends, its open tier 2 proposals with the most evidence become such notifications, as many as the
charter's `max_decisions_per_day` leaves room for that day in the charter's time zone. A revert the hub proposes once
the figures of a merged change got worse (tier 1, `docs/hub.md`, "The Curator's ledger, outcomes and circuit breaker")
becomes one too, whatever room the day has left. Answering the proposal
(`evo-agents hub curator proposal accept|reject|defer`) reads it, and `GET /v1/me/notifications/count` counts the
ones still open as `open_proposals` (`docs/hub.md`, "The Curator's review").

Every member has the web channel without a row of `notification_channels`: a delivery without a channel is the
web's. A channel of another kind is a row, with what it needs in `config` (for Telegram, the chat it sends to).

The job `hub.deliver_notifications` runs every minute in the hub's worker, through the same procrastinate queue as
the other jobs (`docs/hub.md`). It takes the pending deliveries whose `next_at` has passed, with `FOR UPDATE SKIP
LOCKED`, and hands each to the class `CHANNELS` names for its channel's kind. The web's class marks a delivery
delivered at once, since the web reads notifications from the table. A class that fails leaves the delivery pending
with `last_error` and a later `next_at`, backing off (1 minute after the first failure, doubling up to an hour), and
the fifth failure (`MAX_DELIVERY_ATTEMPTS`) marks it `failed`. A class may say more: the service asked to wait
(`RetryAfter`, such as Telegram's 429), which moves `next_at` that far without counting a failed try; the notification
can never go out there (`Undeliverable`), which fails the delivery at once; or the channel no longer reaches its member
(`ChannelGone`, such as a blocked bot), which also turns the channel off with the reason in its `config`
(`disabled_reason`); for a push device, `ChannelGone` removes that device and its deliveries. A channel kind with no
class in the registry fails its deliveries with that reason, so a hub that drops a channel does not retry forever. A
class that answers the id its service gave the message has it kept as the delivery's `external_id` (schema 0015), so a
reply to that message finds its notification.

Before a channel of a row gets a notification, the job checks what stands at that moment, not when the notification
was stored. A member who no longer holds a grant on the notification's project gets nothing of it there: the delivery
fails with that reason and the channel stays. A channel bound to the web session that linked it (`token_id`, schema
0016), as every Telegram chat is, lives no longer than that session: once the session is signed out, revoked or
expired, the delivery fails and the channel is turned off with that reason, until the member links it again. A push
device gets the same checks: no grant fails the delivery and keeps the device, and a device whose sign-in ended is
removed. Its quiet hours come after them (Push to devices, below).

Adding a channel takes a class with one method, which sends one notification to one channel's `config` or raises,
and its entry in `CHANNELS`; the tables, the outbox and the job stay as they are. The routes that link a channel to
a member belong to that channel, like the Telegram ones below.

A notification is read when its owner opens it on the web, marks it read (`POST /v1/me/notifications/read`, by ids or
all), or answers its decision. The web shows the unread count on a bell in the top bar and the notifications in the
Inbox, open decisions and the Curator's open tier 2 proposals first; a proposal opens there in a sheet with its
evidence, and an admin of its project answers it. The audit row `notification.read` names the notifications, not their
text.

## Routes

`evo_agents/hub/server/decisions.py` and `evo_agents/hub/server/notifications.py` hold them, with
`output_notices.py` (the settings), `push.py` (push devices) and `today.py` (Today) in the same directory.

For the worker holding a plan run (or, for `decisions`, an author run), with its `evw_` token and the protocol header
(`docs/workers.md`); any other run, a run of one step, a review run and a judge run included, gets 404:

| Route | Body | Answer |
| --- | --- | --- |
| `POST /v1/worker/runs/{id}/decisions` | `category`, `question` (at most 2,000 characters), `context` (markdown, at most 16 KiB), `options` (2 to 6 of `{key, label, description}`, keys unique), `recommended` (a key), `step_key` (a step of the plan), and, each optional, `recommended_reason`, `reversible`, `undo` and `evidence` (Why the agent recommends, above) | 201, the decision; 422 for a category not listed above (a chat run's is `triage` alone), a step the plan does not have, a step of an author run that has put no plan, a reason without `recommended`, `undo` without `reversible` true, or a ref of evidence in no form above; 409 when the run has 20 decisions open |
| `POST /v1/worker/runs/{id}/notices` | `kind` (of `NOTICE_KINDS`), `title` (one line, at most 200 characters), `body` (at most 16 KiB), `repo` (one of the run's repos), `branch`, `commits` (at most 100) | 201, the notification |

A decision leaves a `system` event in the run's log, and so does a notice. The hub sends `plan_finished` itself when a
plan run ends `done` with every step of its plan done (with the steps it reported), and `run_failed` when one fails
(with the error), its last attempt lost included.

For members, with a web session or a machine token:

| Route | Who | What it does |
| --- | --- | --- |
| `GET /v1/projects/{p}/decisions` | readers of the decision's run: of its plan, or of the project's runs on no plan for an author run without one | the project's decisions, newest first, filtered by `state` (repeatable), `run_id` and `plan_id`, with `limit` and `offset` |
| `GET /v1/projects/{p}/decisions/{id}` | readers of the decision's run, as above | one decision: its options, the recommended one and `recommended_reason`, `reversible`, `undo` and `evidence` (`[]` for none), its state, the answer, `answer_run_id` (the run whose inbox took the answer) and when the agent got it (`delivered_at`) |
| `POST /v1/projects/{p}/decisions/{id}/answer` | the person it is asked of (`asked_of`, above), else the run's owner, who still holds writer | `{"option": KEY, "text": "..."}`, one or both; answers the decision |
| `GET /v1/me/notifications` | the member | their notifications, open decisions and proposals first, then newest first, filtered by `unread`, `kind`, `project`, `run` and `q` (words anywhere in the title or the body, at most 200 characters, `%` and `_` taken as they are), with `limit` and `offset` |
| `GET /v1/me/notifications/count` | the member | `{"unread": N, "open_decisions": M, "open_proposals": P}`, for the bell |
| `POST /v1/me/notifications/read` | the member | `{"ids": [...]}` or `{"all": true}`; answers how many it marked read and how many are left unread |
| `GET /v1/me/notification-settings` | the member | each kind of notice they may turn off (`run_outputs`), and whether it is on; `hide_details`; `tiers`, each kind of notification with its tier and whether they `chosen` it; `quiet_hours` (`{start, end}` or null); `time_zone` and `time_zone_chosen`; `digest_at` (Push to devices, below) |
| `PUT /v1/me/notification-settings` | the member | any of `notices` (`[{"kind": "run_outputs", "on": false}]`), `hide_details`, `tiers` (`[{"kind": "decision", "tier": "brief"}]`, a tier of null going back to the hub's default), `quiet_hours` (`{"start": "22:00", "end": "07:00"}`, null for none), `time_zone` (an IANA name Postgres knows, else 422; null for the hub's) and `digest_at` (`HH:MM`, null for the hub's); sets what it names, the rest stays; a body that names none of them is 422. Audited `notification.settings` with the choices |
| `POST /v1/me/push/devices` | the member, as the sign-in it calls with | adds a push device of theirs, or refreshes it: `{"kind": "webpush", "subscription": PushSubscription.toJSON()}`, `{"kind": "apns", "token", "topic", "environment": "sandbox"\|"production"}` or `{"kind": "fcm", "token"}`, each with an optional `label`; 201, the device; 409 at 20 devices; 422 for a Web Push endpoint that is no push service's |
| `GET /v1/me/push/devices` | the member | their devices that push reaches, newest first: `id`, `kind`, `label`, `created_at`, `last_sent_at` (when a push last reached it) and `this_session` (this sign-in added it) |
| `DELETE /v1/me/push/devices/{id}` | the member | removes one of their devices and the pushes waiting for it; 204, or 404 |
| `POST /v1/me/push/devices/{id}/test` | the member | pushes a test notification to it now, quiet hours or not: `{sent, removed, error}`; a device its push service no longer knows is removed; 503 while the sender of its kind is off |
| `GET /v1/push/config` | anyone, without a sign-in | `{"vapid_public_key": ..., "senders": {"webpush", "apns", "fcm"}}`: the key a browser subscribes with (null while Web Push is off), and which senders are on |
| `GET /v1/me/today` | the member | Today, worked out at this call (Today and its summary push, below) |
| `POST /v1/me/today/seen` | the member | `{"seen_at": ...}`, or `{}` for now: moves their seen mark forward, never back and never past now; answers the mark |

The answer takes the plan's dispatch lock (for a decision with a plan) and the run's row, then: the decision is `answered`, its notification read,
and the answer goes to an inbox as a message that names the decision. A run that is queued or held (waiting
included) gets it in its own inbox. A parked run is resumed: the hub queues a new plan run pinned to the parked run's
worker, at the plan's current revision, with its session, repos, model, timeout and the agent time used so far, and
`resume_of_run_id` naming it; the parked run is `done` with the reason `resumed as #N`, its other open decisions move
to the new run, and the answer goes to the new run's inbox. A parked author run is resumed so only once the answer
leaves none of its decisions open; until then the answer waits in the parked run's inbox, and the run that resumes it
takes every message there. The inbox message reads `Answer to decision #N (category):
question`, then `Chosen option: KEY, LABEL.` and the owner's text.

A notification links to a page of the hub's web: a decision to `/inbox?decision=ID`, a proposal to
`/inbox?proposal=ID`, a notice of a run to the run's page (`/p/{p}/runs/{id}`; `author_waiting` to its chat,
`/p/{p}/runs/{id}?tab=chat`), `curator_brief` and
`curator_paused` to the project's Curator page (`/p/{p}/curator`), `release` to the project's Releases page
(`/p/{p}/releases`), `merge_paused` and a notice of the merge queue to the project's Merge queue page
(`/p/{p}/merge-queue`), `pm_daily` and `pm_escalation` to the project's pm page (`/p/{p}/x/pm`), `loop_paused` to
the project's Loop page (`/p/{p}/loop`), `goal_review` and `loop_brief` to the project's decision board
(`/p/{p}/decide`), `plugin_notice` to the link its plugin gave (default: the plugin's page, else the project), a chat's notice to the chat (`/chats/{id}`,
with `?message=ID` for a mention), and `today` to Home (`/`), which opens with Today. A member sees only the
notifications of projects they hold a grant on.

## Push to devices

A question an agent asks has to reach its person while they are away from the hub. Push is the hub's own code, not a
plugin: `push_store.py` keeps the devices, `push_tiers.py` decides what is pushed and when, `push_senders.py` (Web
Push) and `push_native.py` (APNs and FCM) send, all in `evo_agents/hub/server/`, and `evo_agents/hub/push_payload.py`
writes what a push says. Deliveries to devices go through the outbox above, with its job, backoff and checks.

### Devices

A member has push devices of three kinds (`push_devices`, schema 0149):

| Kind | What the device is | Its address, and what its sender needs besides |
| --- | --- | --- |
| `webpush` | a browser's Web Push subscription, as `PushSubscription.toJSON()` gives it | the endpoint, which must be `https` on port 443 of `fcm.googleapis.com`, `android.googleapis.com`, `push.services.mozilla.com`, `push.apple.com` or `notify.windows.com` (or a subdomain), checked when it is added and before each send, so a member cannot make the hub post into its own network; the browser's P-256 key `p256dh` and the 16-byte secret `auth` |
| `apns` | an app on an iPhone or iPad, through Apple's push service | the device token, in hex; the app's topic (its bundle id) and its environment, `sandbox` or `production` |
| `fcm` | an app on an Android phone, through Firebase Cloud Messaging | the registration token |

The hub takes all three kinds even while their sender is off; a push to such a device then fails at once with the
variable it needs. A device is bound to the sign-in that added it, of any kind but a worker's token: once that token is
signed out, revoked or expired, the device leaves the member's list and is removed at its next delivery. Adding the
same kind and address again refreshes it; adding one another member has moves it to the caller, since an address
speaks for one member. A member has at most 20 devices (409 for the 21st). Adding and removing are audited as
`push_device.add` and `push_device.remove`, with the device's id, kind and login, never its endpoint or token; a
device the hub removes because its push service no longer knows it is audited `by=hub`.

### Tiers

Each kind of notification (`decision`, `proposal` and each notice kind of `NOTICE_KINDS`, keyed by the notice's kind)
is of one tier for each member:

| Tier | What it does |
| --- | --- |
| `interrupt` | pushed at once to every device of the member that reaches them, outside their quiet hours |
| `brief` | not pushed; Today and the Inbox show what changed |
| `off` | not pushed; for `today`, no summary push either |

The tier decides push alone. The Inbox, the web's bell and channels such as Telegram get every notification whatever
its tier, and a notice the member turned off (`run_outputs`, above) is still not sent anywhere. Until a member chooses
a tier for a kind, the hub's default holds:

- `interrupt` for a decision asked of the member (the person who answers it, `asked_of` above), a `run_failed` of a
  run they dispatched (not a run of the Curator's night shift, which its morning brief reports), a `release` notice of
  a release that went wrong (any event but `published`) and every `merge_paused`, the breaker that stops a merge queue
  and its deploys;
- `brief` for every other notification, a decision asked of someone else included, and for a kind added later, which
  needs no change of code to get it.

A tier the member chooses holds for every notification of that kind, without the default's conditions: `decision` set
to `interrupt` pushes every decision of theirs, not only those asked of them. `PUT /v1/me/notification-settings` with
`{"tiers": [{"kind": "decision", "tier": null}]}` goes back to the default, and `GET` shows each kind with its tier and
whether the member chose it (`chosen`). `notification_preferences` (schemas 0149 and 0150) keeps what a member chose;
a member without a row has the defaults.

### Quiet hours

A member may set quiet hours, `start` to `end` in `HH:MM`, past midnight when the end is not after the start, in their
time zone: an IANA name that Postgres knows, checked as the Curator's charter checks its own, and the hub's
`EVO_HUB_WEB_TIME_ZONE` (default `Asia/Ho_Chi_Minh`) until they choose one; the web offers the browser's. The job
`hub.deliver_notifications` checks them as it takes each delivery to a device, not when the notification is stored, so
quiet hours set meanwhile hold. Inside them a push waits until they end, without counting a try, with one exception: a
decision whose run parks for want of an answer within two hours is pushed at once, and one whose run parks later waits
at most until two hours before it parks. A run parks `EVO_HUB_DECISION_WAIT_SECONDS` after it starts waiting (a chat
run after 10 minutes); a run still working counts as if it started waiting now, and a local run, a parked run and an
ended one park no more. A push of a decision that was answered or closed by the time it would go out is not sent
(`notifications.DECISION_CLOSED`), so a push held overnight never asks a question nobody asks any more. A test push
ignores quiet hours. A time zone Postgres no longer knows counts as UTC, with a warning in the log.

### What a push says

Every sender carries the same payload (`push_payload.payload`): JSON of `title`, `body`, `url` (the absolute link of
the hub's page), `tag` (one per notification, so a later push of it replaces the earlier one), `kind` and `project`,
at most 3,072 bytes, under the 4 KB that Web Push, APNs and FCM each take, its body cut first and then its title. The
words are English, as the hub's web is.

- **Hidden on the lock screen.** A notification of a project whose hub sink the clearance keeps from Telegram (What
  never goes to Telegram, below: the same rule, failing closed the same way), a decision or proposal of no project,
  and every push to a member who turned on Hide details (`hide_details`), carry only the kind of thing as the title
  ("Decision needed", "Proposal to review", "Run failed" and the other words of `telegram.NOTICE_WORDS`), the project
  as the body, and the link.
- **Otherwise** a push carries the notification's title, and for a decision its question, at most 300 characters, and
  `Recommended: <label>` when the agent recommends an option; for the summary push of Today its sentence; for anything
  else the kind of thing and the project.
- **Never**, whatever the project: a decision's context, a file or its link, an output, an image, a token or a code.
  The senders do not read them.

Tapping a push opens the hub at its link; answering from the notification itself is not built, so a question is
answered on the hub's page it opens.

### The senders

`GET /v1/push/config` says which senders are on. Each is off without its variables (`docs/hub-deployment.md`, Push to
browsers and phones), and the rest of the outbox, the web and Telegram included, runs as before: a delivery to a
device whose sender is off fails at once with what to set, and its test route answers 503.

| Sender | How it sends | When the device goes |
| --- | --- | --- |
| Web Push | the payload encrypted for the subscription (RFC 8291, `aes128gcm`) and signed in with the hub's VAPID key (RFC 8292), `TTL` 86400 and `Urgency: high`, to the subscription's endpoint | 404 or 410 |
| APNs | `POST /3/device/<token>` over HTTP/2, the only protocol APNs takes, on `api.push.apple.com` or, for a device of environment `sandbox`, `api.sandbox.push.apple.com`; the device's topic, push type alert, priority 10, expiry a day ahead and the notification's tag as collapse id; a provider token signed with ES256 by the hub's key, made again every 50 minutes, since Apple refuses one older than an hour and new ones more often than every 20 minutes. The body is `aps` (alert, sound default, `thread-id` the project) with `url`, `tag`, `kind` and `project` | 410 with any reason, or the reason `BadDeviceToken` or `Unregistered` |
| FCM | `POST /v1/projects/<project>/messages:send` of the HTTP v1 API with an OAuth access token of the hub's service account (a JWT signed with RS256, exchanged at `https://oauth2.googleapis.com/token`, kept until 5 minutes before it expires); `notification` (title, body), `data` (`url`, `tag`, `kind`, `project`) and `android` (priority high, TTL a day, the tag) | the error code `UNREGISTERED` |

A device the push service no longer knows is removed with its deliveries, alone. A 429 waits for its `Retry-After`
without counting a try. Any other 4xx (`DeviceTokenNotForTopic`, `INVALID_ARGUMENT`, `SENDER_ID_MISMATCH` among
them) fails the delivery and keeps the device, so a wrong key on the hub cannot wipe every member's devices. An APNs
`ExpiredProviderToken` or an FCM 401 drops the token the process kept and tries again with the outbox's backoff, as a
5xx or a network error does. Google's token endpoint answering 4xx fails the delivery at once, naming
`EVO_HUB_FCM_SERVICE_ACCOUNT`. Each process keeps its provider and access tokens, each registered as a secret of the
log while it is kept. Tests run both native senders against fake servers in the process; no test calls Apple or
Google.

### Today and its summary push

`GET /v1/me/today` (`today.py`) is worked out at each call, over every project the caller holds a grant on, with the
checks of `GET /v1/me/overview`: a decision, run or plan of a plan whose label the caller's grant does not reach is
neither listed nor counted. It answers:

- `needs_you`: the open decisions asked of the caller, the oldest first, at most 20, each with when its run parks
  (`parks_at`), and `needs_you_count`, all of them;
- `items`: at most 8 things that changed in the window, the first in the order they happened, each with its `kind`, a
  `title`, a `body`, its moment `at`, its `project` and a `link`: `release` (a release of a project published),
  `step_done` (a step of a plan the caller reads that is done now and was not in the revision the hub held at the start
  of the window; a plan first written in the window starts from its first revision), `run_failed` and `run_parked` (a
  run the caller dispatched that failed, was lost or parked; the night shift's runs are left to the Curator's morning
  brief), `problem` (an open problem the caller owns whose metric differs from seven days ago, measured as the Loop
  measures a goal's), `cost` (what the caller's runs that ended in the window cost, one item a project) and `proposal`
  (an open proposal of the Curator in a project the caller is an admin of, who may answer it);
- `caught_up`, whether the items are everything that changed in the window, and `seen_until`, the mark to post once
  every item listed was shown: the moment of the answer when caught up, else the moment of the last item;
- `summary`, one sentence made from English templates, no model, and `since` and `seen_at`.

The window starts at the caller's seen mark (`today_marks.seen_at`, schema 0151). Without a mark it is the last 24
hours, and a mark older than seven days counts from seven days ago. `POST /v1/me/today/seen` moves the mark forward to
its `seen_at` (now when left out, never past now); a later mark stays where it is. Home on the web opens with Today
and posts `seen_until` once the end of the list has been on screen.

The job `hub.today_digest` runs every minute (`today_digest.py`, through `brief.send_today_digests`). A member gets a
summary push when they have a push device that reaches them, have been away from the hub for 8 hours (by
`users.last_seen_at` and by their seen mark, since the hub writes `last_seen_at` at most once a day a token), did not
set the tier of `today` to `off`, and it is their `digest_at` (default `07:30`) in their time zone, or up to three hours
later. The day is claimed in `today_marks.digest_on` before Today is worked out, so a second pass sends nothing and a
member gets at most one a local day. When their Today has an item, they get a notice `today`: no project, the title
`Today on the hub`, the sentence as its body, a link to Home, read when it is stored, and delivered to their devices
alone. A day with no item sends nothing. Quiet hours hold it as any push, and Hide details leaves only `Today on the
hub`.

`GET /v1/me/overview` counts in `waiting_on_you`, and marks `yours`, the decisions asked of the caller, and names the
person each decision is asked of (`asked_of`); Home's Needs you reads it.

### On the web

The web serves its service worker at `/sw.js` without a session (the proxy leaves it out of its matcher). It shows a
push as one notification tagged by its notification; a tap focuses the hub's tab already on that page, or takes the
hub's first tab there, or opens one, and a link of another origin keeps only its path on the hub's. When the browser
replaces a subscription, the worker subscribes again with the hub's key and sends the new one to
`POST /v1/me/push/devices`. It has no fetch handler and caches nothing.

`/settings/notifications`, linked from the user menu and from the Inbox's Notices dialog, holds a member's settings:
push on this browser, turned on from one button, the only place the web asks the browser for permission; the
member's devices, each with a test push and Remove; the tier of each kind; quiet hours and the time zone; the time of
the summary push; and Hide details on the lock screen. Safari on an iPhone or iPad offers Web Push only to a web app
added to the Home Screen, and asks for permission only from a person's tap: outside such an app the page shows how to
add it. The Inbox's decision card and the phone's decision screen show the agent's reason for its pick, whether and how
the act can be undone, and its evidence as links.

## Telegram

Telegram is a core plugin, `telegram`, of the distribution `evo-agents-core-plugins` (repository
[maycuatroi1/evo-agents-core-plugins](https://github.com/maycuatroi1/evo-agents-core-plugins)), which
`deploy/hub/core-plugins.txt` pins into every image of the hub (`docs/plugins.md`, section Core plugins): the hub finds
it in the entry point group `evo_agents.plugins` and reaches it through its manifest only, and the plugin reaches the
hub through the API of core plugins alone (`docs/plugins.md`, The core plugin API, version 1.1). Its
`evo_core_plugins/telegram/model.py` holds the bot's commands, its button data and its messages, and
`evo_core_plugins/telegram/server.py` the Bot API client, the channel's class, the webhook and the routes, at the paths
they had before Telegram was a plugin; its tests are in that repository. The hub keeps in `evo_agents/hub/telegram.py`
what it says itself: the link codes, what may reach a chat (which the group chats share) and the words of a minimal
message, and in evo-agents the Inbox's Telegram dialog, `hub admin telegram`, the variables below and the end-to-end
test `web/e2e/telegram.spec.ts`. The plugin calls the Bot API with httpx; no bot library.

### One bot of the hub

The hub admin creates the bot with BotFather and sets `EVO_HUB_TELEGRAM_BOT_TOKEN` and
`EVO_HUB_TELEGRAM_WEBHOOK_SECRET` (1 to 256 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`). The hub checks their shape
as it starts and the plugin reads them from the process's environment, checking it again. Without either the channel
is off: linking answers 503, the webhook 404, a delivery to a Telegram channel fails at once, and the hub runs on. Members
only link their chat. The webhook is a path of the hub's own domain, `/v1/telegram/webhook`, which the reverse proxy
already sends to the api; `evo-agents hub admin telegram --set-webhook` (a hub admin) points the bot there with
`setWebhook`, the secret as `secret_token`, and the updates `message` and `callback_query`, and `evo-agents hub admin
telegram` shows where it points, with Telegram's last error.

### Linking a chat

1. On the web, the member opens the Inbox and its Telegram dialog and asks for a link (`POST /v1/me/telegram/link`).
   Only a web session may: a machine token gets 403, so a token that leaked cannot link a chat of its own. The hub
   makes a one-time code of 32 characters of `A-Z`, `a-z`, `0-9`, `_` and `-`, keeps its SHA-256, the member and the
   session (`channel_link_codes` of kind `telegram`, schemas 0015, 0016 and 0063), lets it live 10 minutes, drops the
   member's codes not used yet, and answers the link `https://t.me/<bot>?start=<code>`.
2. The member opens the link, and Telegram sends the bot `/start <code>` from that member's private chat.
3. The webhook finds the code by its hash. A used, expired or unknown code, a code whose session has ended, or a chat
   that is not private, gets one reply that says only that the link did not work, whichever it was. A right code is
   used up, and the hub stores a channel of kind `telegram` whose `config` holds the chat id, the Telegram user id and
   username, bound to the session that made the code, and replies with the hub and the member it linked. The audit
   row `telegram.link` names the member.

A member has one Telegram channel and a chat speaks for one member: linking again replaces both. `GET /v1/me/telegram`
says whether the hub has a bot and where the member's link stands, turned off with its reason included.

The chat lives as long as the web session that linked it. Signing out of that session, revoking it (its member on the
tokens page, or a hub admin), or its expiry after 90 days without use ends the chat: the hub sends it nothing more,
takes no answer from it, and says on the web that the session ended; the member links it again from a live session.
A chat speaks for its member with no token and never as a hub admin.

### What a decision looks like there

The message names the run, the project, the category, the plan and the step (for a decision of an author run without
a plan, `Author run #N` and the run's title), asks the question, and lists the options
with their labels, the recommended one marked. Under it, an inline keyboard has one button per option and a button that
opens the decision on the web. A button's `callback_data` carries the decision's id and the option key, `d:<id>:<key>`,
never any text. The context is not sent: the web button leads to it. A tier 2 proposal of the Curator goes out with its
title, tier, kind and lens, the buttons Accept, Reject and Defer 7 days (`p:<id>:<answer>`) and the web's button; its
summary, paths, evidence and draft plan stay on the hub. A notice, the morning brief included, goes out with its kind,
title and body as plain text and the web's button. A decision or proposal that no longer waits for an answer by the
time its message goes out (answered on the web meanwhile, say) goes out as the project, its number, that it no longer
waits and the link, never with its question or summary.

The files of a decision and of a `run_outputs` notice reach the channel in what the hub prepares for it
(`core_plugin_api.prepare`, `docs/plugins.md`): `extra.outputs`, at most 10 files (a media group of Telegram holds 10),
oldest first, each with its id, name, media type, viewer, size, caption and `url`, a presigned GET of its bytes that
works 5 minutes and is made at each try, and `extra.outputs_count`, how many files there are; both keys are left out
when there is no file to send. A file of source `tool` is never among them, not even one a question carries. The
channel sends them from evo-agents-core-plugins 1.1.0 on: one photo album when every file is a photo, otherwise one
group of documents, with the run's link in the caption; a decision sends its files first and then its question with
its buttons as a reply to the first file. Files past 10, past 20 MiB in all or past Telegram's own limits stay on the
hub, and the message says how many. 1.0.0 did not read `extra.outputs` and sent text only.

A message is HTML within Telegram's 4,096 characters. A part that may run long is cut before it is escaped, or between
the tags and entities of its HTML, never inside one; the question of a decision gives way first, and the link, and the
outcome of an answered message, stay whole.

### Answers

Telegram calls the webhook with the header `X-Telegram-Bot-Api-Secret-Token`. The hub compares it with the secret in
constant time and refuses anything else (403) before it reads the body; an update it does not act on gets 200, so
Telegram does not send it again. For a button, it finds the channel by the chat and the Telegram user, while the session
that linked it lives. Before it reads anything of the decision or proposal the button names, it checks that the member
holds a grant on its project: one of another project gets the same answer as one that does not exist, naming neither,
and a project whose messages are restricted (below) takes no answer from Telegram, as its messages have no buttons.
Button data that is not the hub's (an option key a decision cannot have, say) gets a refusal, never an error. Then it
answers as that channel's member through the same code as the web (the API's `answer_decision` and
`answer_proposal`, over `decisions.answer_decision` and `proposals.answer_proposal_as`):
the same checks (only the run's owner answers a decision, an admin of the project a proposal; a run on a worker that
takes runs from the web only is answered on the web), the inbox message, the audit row, which ends `via=telegram` and
names no token, and the run going on. A proposal takes no answer while a hub admin has turned the feature `curator`
off, as on the web. It then calls `answerCallbackQuery`, so the button stops spinning and shows the
outcome or the refusal, and edits the message to show the answer, or that it was answered first, keeping only the web
button. A reply to the decision's message is its answer in text: the hub finds the decision by the chat and the message
id it kept (`external_id`, `<chat>:<message>`), since Telegram numbers messages per chat, with the same checks as a
button. A member who wants an option and text together answers on the web.

### Limits

Telegram's Bot FAQ asks bots to stay near one message per second in one chat and about 30 per second overall. The
channel waits so that no chat gets two messages within a second and no second carries more than 30, and a 429 from
Telegram moves the delivery's `next_at` to the `retry_after` it gives without counting it as a failed attempt. The
webhook takes at most 5 updates per 10 seconds from one chat, in each api process, and drops the rest, answering none
of them. The summary of a burst of notices (more than 20 in 10 minutes as one message) is not built.

### Unlinking

A member unlinks on the web (`DELETE /v1/me/telegram`), which deletes the channel and the deliveries waiting for it.
Sending `/stop` to the bot does the same, whatever became of the session that linked the chat. A hub admin unlinks any
member's chat (`DELETE /v1/admin/users/{login}/telegram`, `evo-agents hub admin telegram-unlink LOGIN`). Each is
audited as `telegram.unlink`, with `by=web`, `by=bot` or `by=admin`. When Telegram answers 403 because the member
blocked the bot, or 400 for a chat it no longer knows, the hub turns the channel off and says so on the web, as it does
when the session that linked the chat ends; linking again turns it on. Losing the grant on a project stops that
project's notifications at their delivery, not the channel; deleting the member deletes their channels.

### What never goes to Telegram

Telegram is a sink outside the hub, so the message holds as little as the owner needs to decide:

- not a decision's context, a proposal's summary, paths or evidence, a run's log or diff, a step's evidence, the plan's
  text or the terminal: the web button leads to them;
- for a project whose hub sink is cleared at or above the level `customer` or the location `domestic-only` on the
  project's own ladder, read as `evo_agents.kg.policy` reads a clearance (a sink that names no location may receive
  any, so it sits at the top of the locations), the project's name, the kind of thing that waits and the link, nothing
  else: no question, title, option, context, diff, file or file name, and no answer button. Its channel's `extra` is
  `{"restricted": true}` alone, so a `run_outputs` notice goes out as the project, the words of its kind (the hub's
  and those of the core plugin telegram from 1.1.0 are `Files from a run`) and the link. It fails closed: a project with
  no hub sink, a clearance without a level, a level or location the ladder lacks, or a ladder without `customer` (or
  `domestic-only`) and a clearance above its lowest rung, is restricted too;
- no image taken out of a tool's result (an output of source `tool`), whatever the project, even when a question
  carries it: the web shows it;
- no token, pairing code or link code, ever;
- no command beyond answering a decision or a proposal: cancelling, approving, dispatching and messages to the agent stay
  on the web and the command line.
