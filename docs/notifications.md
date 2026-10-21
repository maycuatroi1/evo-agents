# Notifications and decisions

A plan run (see [Plan runs](workers.md#plan-runs)) works for hours without anyone watching. Two things in it need
the member who dispatched it, the run's owner: a decision the agent may not take alone, and a push or merge the
owner should know about. The hub turns each into a notification for the owner and hands it to every channel the
owner has turned on. The web was the one channel of 0.4.0; Telegram, at the end of this page, came later as a class
added to the channel registry, with routes of its own and no change to the outbox.

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
| delivery | one notification on one channel, with its attempts | `notification_deliveries` |
| registry | `CHANNELS` in the api: for each channel kind, the class that sends a notification there | code, not a table |

## Decisions

The agent of a plan run asks with `evo-agents worker ask`, and only about these categories (`DECISION_CATEGORIES`). The
prompt of a plan run lists all of them but `deploy` (`PROMPTED_DECISION_CATEGORIES`): merging, deploying and releasing
are the agent's to do, and the route still takes a `deploy` decision from a worker of an older prompt. The route that
takes a decision refuses any other, and the agent decides everything else itself and writes each such choice, with why,
into the evidence of its step on a line that starts with `Decision:`.

| Category | What it covers |
| --- | --- |
| `deploy` | deploying or releasing anything to any environment, a checkpoint step that deploys included |
| `delete_data` | deleting data that is not the run's own scratch: database rows, files, buckets, other people's branches |
| `live_migration` | a migration, backfill or bulk change on real data rather than a test database |
| `external_send` | sending anything to a service outside the machine and the repos' own remotes: mail, chat, issues, third-party APIs |
| `spend_money` | anything that costs money beyond the runtime's own usage: paid APIs, cloud resources, purchases |
| `architecture` | an architectural choice the plan leaves open |
| `scope` | a question of scope the plan leaves open: adding, dropping or reshaping a step or what it delivers |

A decision holds the run, its project, plan and step (the step is optional), the category, a question, a context in
markdown of at most 16 KiB, and 2 to 6 options. An option has a key (letters, digits, `_` and `-`, at most 32
characters, starting with a letter or digit), a label, an optional description, and whether it is the one the agent
recommends; at most one is. The owner answers with an option, with text of their own, or with both.

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
| `author_waiting` | an author run's agent ended its turn and the run waits for its owner's reply in its chat (`docs/workers.md`, "The chat of an author run"), with no decision of the run open; the next one reads the run's earlier ones | the agent's last message, the plan it wrote or revises |
| `chat_mention` | someone mentioned the member in a group chat (`docs/hub.md`, "Group chats"), a person or the agent of a run (`@handle`) | the chat, the sender and the message; in a restricted chat only the chat's name and the link |
| `chat_added` | someone added the member to a group chat; the agent of a run that creates a chat (`evo-agents worker chat create`) tells the person who dispatched the run, and each person it adds | the chat and who added them; in a restricted chat only the chat's name and the link |
| `run_outputs` | a run's agent sent files, or left them in its outputs directory (`docs/hub.md`, "Outputs of runs"), to the run's owner, gathered a minute a run (below) | the run, the files' names, sizes and captions; `details.outputs` their ids, and the Inbox lists them as `outputs` |

A worker sends only the first four (`runs.WORKER_NOTICE_KINDS`); the hub alone sends `curator_brief`,
`curator_paused`, `release`, `merge_paused`, `author_waiting`, `chat_mention`, `chat_added` and `run_outputs`, and
the `merge_default_branch` of its merge queue. A release run that fails sends no
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

A member turns `run_outputs` off for themselves (`runs.MUTABLE_NOTICE_KINDS`, schema 0086's `notification_mutes`):
the hub then stores no such notice for them, on any channel, and their decisions and every other notice still come.
`GET /v1/me/notification-settings` answers each kind a member may turn off with whether it is on, and `PUT` with
`{"notices": [{"kind": "run_outputs", "on": false}]}` sets the kinds it names, audited `notification.settings` with
the kinds and no text.

## Notifications, channels and deliveries

Each decision and each notice becomes, in the transaction that stores it, one notification for the run's owner with a
title, a body, a link to the web page that shows it, and the project, run and decision it belongs to; and one
delivery of it for each channel the owner has turned on. The table is the outbox: nothing is sent while the
transaction is open, and a hub that stops between the two loses nothing.

| Table | Columns |
| --- | --- |
| `notifications` | user, kind (`decision`, `notice` or `proposal`), the decision, the notice's kind and details (repo, branch, commits), or the proposal, project, run, title, body, link (a path of the hub's site), `created_at`, `read_at` |
| `notification_channels` | user, kind (not `web`, at most one of a kind per member), `config` (JSON the channel's class reads), `enabled`, `created_at` |
| `notification_deliveries` | notification, channel (none for the web), state (`pending`, `delivered`, `failed`), attempts, `next_at`, `last_error`, `delivered_at` |

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
(`disabled_reason`). A channel kind with no class in the registry fails its deliveries with that reason, so a hub that
drops a channel does not retry forever. A class that answers the id its service gave the message has it kept as the
delivery's `external_id` (schema 0015), so a reply to that message finds its notification.

Before a channel of a row gets a notification, the job checks what stands at that moment, not when the notification
was stored. A member who no longer holds a grant on the notification's project gets nothing of it there: the delivery
fails with that reason and the channel stays. A channel bound to the web session that linked it (`token_id`, schema
0016), as every Telegram chat is, lives no longer than that session: once the session is signed out, revoked or
expired, the delivery fails and the channel is turned off with that reason, until the member links it again.

Adding a channel takes a class with one method, which sends one notification to one channel's `config` or raises,
and its entry in `CHANNELS`; the tables, the outbox and the job stay as they are. The routes that link a channel to
a member belong to that channel, like the Telegram ones below.

A notification is read when its owner opens it on the web, marks it read (`POST /v1/me/notifications/read`, by ids or
all), or answers its decision. The web shows the unread count on a bell in the top bar and the notifications in the
Inbox, open decisions and the Curator's open tier 2 proposals first; a proposal opens there in a sheet with its
evidence, and an admin of its project answers it. The audit row `notification.read` names the notifications, not their
text.

## Routes

`evo_agents/hub/server/decisions.py` and `evo_agents/hub/server/notifications.py` hold them.

For the worker holding a plan run (or, for `decisions`, an author run), with its `evw_` token and the protocol header
(`docs/workers.md`); any other run, a run of one step, a review run and a judge run included, gets 404:

| Route | Body | Answer |
| --- | --- | --- |
| `POST /v1/worker/runs/{id}/decisions` | `category`, `question` (at most 2,000 characters), `context` (markdown, at most 16 KiB), `options` (2 to 6 of `{key, label, description}`, keys unique), `recommended` (a key), `step_key` (a step of the plan) | 201, the decision; 422 for a category not listed above (a chat run's is `triage` alone), a step the plan does not have, or a step of an author run that has put no plan; 409 when the run has 20 decisions open |
| `POST /v1/worker/runs/{id}/notices` | `kind` (of `NOTICE_KINDS`), `title` (one line, at most 200 characters), `body` (at most 16 KiB), `repo` (one of the run's repos), `branch`, `commits` (at most 100) | 201, the notification |

A decision leaves a `system` event in the run's log, and so does a notice. The hub sends `plan_finished` itself when a
plan run ends `done` with every step of its plan done (with the steps it reported), and `run_failed` when one fails
(with the error), its last attempt lost included.

For members, with a web session or a machine token:

| Route | Who | What it does |
| --- | --- | --- |
| `GET /v1/projects/{p}/decisions` | readers of the decision's run: of its plan, or of the project's runs on no plan for an author run without one | the project's decisions, newest first, filtered by `state` (repeatable), `run_id` and `plan_id`, with `limit` and `offset` |
| `GET /v1/projects/{p}/decisions/{id}` | readers of the decision's run, as above | one decision: its options, the recommended one, its state, the answer, `answer_run_id` (the run whose inbox took the answer) and when the agent got it (`delivered_at`) |
| `POST /v1/projects/{p}/decisions/{id}/answer` | the run's owner, who still holds writer | `{"option": KEY, "text": "..."}`, one or both; answers the decision |
| `GET /v1/me/notifications` | the member | their notifications, open decisions and proposals first, then newest first, filtered by `unread`, `kind` and `project`, with `limit` and `offset` |
| `GET /v1/me/notifications/count` | the member | `{"unread": N, "open_decisions": M, "open_proposals": P}`, for the bell |
| `POST /v1/me/notifications/read` | the member | `{"ids": [...]}` or `{"all": true}`; answers how many it marked read and how many are left unread |
| `GET /v1/me/notification-settings` | the member | each kind of notice they may turn off (`run_outputs`), and whether it is on |
| `PUT /v1/me/notification-settings` | the member | `{"notices": [{"kind": "run_outputs", "on": false}]}`; turns the kinds it names on or off, the others stay |

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
(`/p/{p}/merge-queue`), and a chat's notice to the chat (`/chats/{id}`,
with `?message=ID` for a mention). A member sees only the notifications of projects
they hold a grant on.

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
channel sends the images as photos and the other files as documents, gathered, with the run's link, from the release
of the core plugin telegram that reads `extra.outputs`; evo-agents-core-plugins 1.0.0 does not read it and sends the
notice's kind, title, body and link as text, and a decision's question and buttons without its files.

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
  are `Files from a run`; evo-agents-core-plugins 1.0.0 says `A notice`) and the link. It fails closed: a project with
  no hub sink, a clearance without a level, a level or location the ladder lacks, or a ladder without `customer` (or
  `domestic-only`) and a clearance above its lowest rung, is restricted too;
- no image taken out of a tool's result (an output of source `tool`), whatever the project, even when a question
  carries it: the web shows it;
- no token, pairing code or link code, ever;
- no command beyond answering a decision or a proposal: cancelling, approving, dispatching and messages to the agent stay
  on the web and the command line.
