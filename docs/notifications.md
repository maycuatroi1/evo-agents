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
| decision | a question the agent of a plan run asks its owner, with 2 to 6 options, open until it is answered, expires or is cancelled | `decisions` |
| notice | something the owner should know and need not answer, such as a push to a default branch | none of its own: it is a notification of kind `notice` |
| notification | one decision or notice for one member, read or not | `notifications` |
| channel | a place where a member gets notifications: the web, later Telegram | `notification_channels` |
| delivery | one notification on one channel, with its attempts | `notification_deliveries` |
| registry | `CHANNELS` in the api: for each channel kind, the class that sends a notification there | code, not a table |

## Decisions

The agent of a plan run asks with `evo-agents worker ask`, and only about these categories (`DECISION_CATEGORIES`).
The prompt of a plan run lists them, the route that takes a decision refuses any other, and the agent decides
everything else itself and writes each such choice, with why, into the evidence of its step on a line that starts
with `Decision:`.

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

Only the run's owner answers, through the web or with a machine token of their own: another member gets 403,
someone without a grant on the project 404, and the worker's own token 403, so the agent cannot answer itself. A
decision that is no longer open gets 409. The answer goes into the run's inbox as a message that names the decision
id, and the heartbeat hands it to the agent like any message of the owner's (`docs/workers.md`, The inbox). The audit
row `decision.answer` names the decision and the option, never the text.

## Notices

A notice needs no answer (`NOTICE_KINDS`):

| Kind | Sent when | Holds |
| --- | --- | --- |
| `push_default_branch` | a plan run pushed to a repo's default branch, which the plan names for that repo | the repo, the branch, the commits |
| `merge_default_branch` | a plan run merged into such a branch | the repo, the branch, the commits |
| `plan_finished` | a plan run ended with every step of its plan done | the plan, the steps the run did |
| `run_failed` | a plan run failed, or its last attempt was lost | the run, the error |
| `curator_brief` | the charter's `brief_at` came, in its time zone: the Curator's morning brief to the owner of the night shift's schedule (`docs/hub.md`, "The Curator's review") | the night's runs and cost, its review run, merges, runs waiting for approval, open decisions and proposals, the worker on duty's last heartbeat |

A worker sends only the first four (`runs.WORKER_NOTICE_KINDS`); the hub alone sends `curator_brief`. The daemon
sends `push_default_branch` itself for the pushes of `evo-agents worker step`; for a push or merge the
agent makes itself, the prompt tells it to run `evo-agents worker notify`. A run of one step never pushes a default
branch and sends no notice.

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
charter's `max_decisions_per_day` leaves room for that day in the charter's time zone; answering the proposal
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

For the worker holding a plan run, with its `evw_` token and the protocol header (`docs/workers.md`); any other run,
a run of one step included, gets 404:

| Route | Body | Answer |
| --- | --- | --- |
| `POST /v1/worker/runs/{id}/decisions` | `category`, `question` (at most 2,000 characters), `context` (markdown, at most 16 KiB), `options` (2 to 6 of `{key, label, description}`, keys unique), `recommended` (a key), `step_key` (a step of the plan) | 201, the decision; 422 for a category not listed above or a step the plan does not have; 409 when the run has 20 decisions open |
| `POST /v1/worker/runs/{id}/notices` | `kind` (of `NOTICE_KINDS`), `title` (one line, at most 200 characters), `body` (at most 16 KiB), `repo` (one of the run's repos), `branch`, `commits` (at most 100) | 201, the notification |

A decision leaves a `system` event in the run's log, and so does a notice. The hub sends `plan_finished` itself when a
plan run ends `done` with every step of its plan done (with the steps it reported), and `run_failed` when one fails
(with the error), its last attempt lost included.

For members, with a web session or a machine token:

| Route | Who | What it does |
| --- | --- | --- |
| `GET /v1/projects/{p}/decisions` | readers of the run's plan | the project's decisions, newest first, filtered by `state` (repeatable), `run_id` and `plan_id`, with `limit` and `offset` |
| `GET /v1/projects/{p}/decisions/{id}` | readers of the run's plan | one decision: its options, the recommended one, its state, the answer, `answer_run_id` (the run whose inbox took the answer) and when the agent got it (`delivered_at`) |
| `POST /v1/projects/{p}/decisions/{id}/answer` | the run's owner, who still holds writer | `{"option": KEY, "text": "..."}`, one or both; answers the decision |
| `GET /v1/me/notifications` | the member | their notifications, open decisions and proposals first, then newest first, filtered by `unread`, `kind` and `project`, with `limit` and `offset` |
| `GET /v1/me/notifications/count` | the member | `{"unread": N, "open_decisions": M}`, for the bell |
| `POST /v1/me/notifications/read` | the member | `{"ids": [...]}` or `{"all": true}`; answers how many it marked read and how many are left unread |

The answer takes the plan's dispatch lock and the run's row, then: the decision is `answered`, its notification read,
and the answer goes to an inbox as a message that names the decision. A run that is queued or held (waiting
included) gets it in its own inbox. A parked run is resumed: the hub queues a new plan run pinned to the parked run's
worker, at the plan's current revision, with its session, repos, model, timeout and the agent time used so far, and
`resume_of_run_id` naming it; the parked run is `done` with the reason `resumed as #N`, its other open decisions move
to the new run, and the answer goes to the new run's inbox. The inbox message reads `Answer to decision #N (category):
question`, then `Chosen option: KEY, LABEL.` and the owner's text.

A notification links to a page of the hub's web: a decision to `/inbox?decision=ID`, a notice to the run's page
(`/p/{p}/runs/{id}`). A member sees only the notifications of projects they hold a grant on.

## Telegram

`evo_agents/hub/telegram.py` holds the model (link codes, button data, what may reach a chat, the messages) and
`evo_agents/hub/server/telegram.py` the Bot API client, the channel's class, the webhook and the routes. The hub calls
the Bot API with httpx; no bot library.

### One bot of the hub

The hub admin creates the bot with BotFather and sets `EVO_HUB_TELEGRAM_BOT_TOKEN` and
`EVO_HUB_TELEGRAM_WEBHOOK_SECRET` (1 to 256 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`). Without either the channel is
off: linking answers 503, the webhook 404, a delivery to a Telegram channel fails at once, and the hub runs on. Members
only link their chat. The webhook is a path of the hub's own domain, `/v1/telegram/webhook`, which the reverse proxy
already sends to the api; `evo-agents hub admin telegram --set-webhook` (a hub admin) points the bot there with
`setWebhook`, the secret as `secret_token`, and the updates `message` and `callback_query`, and `evo-agents hub admin
telegram` shows where it points, with Telegram's last error.

### Linking a chat

1. On the web, the member opens the Inbox and its Telegram dialog and asks for a link (`POST /v1/me/telegram/link`).
   The hub makes a one-time code of 32 characters of `A-Z`, `a-z`, `0-9`, `_` and `-`, keeps its SHA-256 and the
   member (`telegram_links`, schema 0015), lets it live 10 minutes, drops the member's codes not used yet, and answers
   the link `https://t.me/<bot>?start=<code>`.
2. The member opens the link, and Telegram sends the bot `/start <code>` from that member's private chat.
3. The webhook finds the code by its hash. A used, expired or unknown code, or a chat that is not private, gets one
   reply that says only that the link did not work, whichever it was. A right code is used up, and the hub stores a
   channel of kind `telegram` whose `config` holds the chat id, the Telegram user id and username, and replies with
   the hub and the member it linked. The audit row `telegram.link` names the member.

A member has one Telegram channel and a chat speaks for one member: linking again replaces both. `GET /v1/me/telegram`
says whether the hub has a bot and where the member's link stands, turned off with its reason included.

### What a decision looks like there

The message names the run, the project, the category, the plan and the step, asks the question, and lists the options
with their labels, the recommended one marked. Under it, an inline keyboard has one button per option and a button that
opens the decision on the web. A button's `callback_data` carries the decision's id and the option key, `d:<id>:<key>`,
never any text. The context is not sent: the web button leads to it. A tier 2 proposal of the Curator goes out with its
title, tier, kind and lens, the buttons Accept, Reject and Defer 7 days (`p:<id>:<answer>`) and the web's button; its
summary, paths, evidence and draft plan stay on the hub. A notice, the morning brief included, goes out with its kind,
title and body as plain text and the web's button.

### Answers

Telegram calls the webhook with the header `X-Telegram-Bot-Api-Secret-Token`. The hub compares it with the secret in
constant time and refuses anything else (403) before it reads the body; an update it does not act on gets 200, so
Telegram does not send it again. For a button, it finds the channel by the chat and the Telegram user, and answers as
that channel's member through the same code as the web (`decisions.answer_decision`, `proposals.answer_proposal_as`):
the same checks (only the run's owner answers a decision, an admin of the project a proposal; a run on a worker that
takes runs from the web only is answered on the web), the inbox message, the audit row, which ends `via=telegram` and
names no token, and the run going on. It then calls `answerCallbackQuery`, so the button stops spinning and shows the
outcome or the refusal, and edits the message to show the answer, or that it was answered first, keeping only the web
button. A reply to the decision's message is its answer in text: the hub finds the decision by the message id it kept
(`external_id`). A member who wants an option and text together answers on the web.

### Limits

Telegram's Bot FAQ asks bots to stay near one message per second in one chat and about 30 per second overall. The
channel waits so that no chat gets two messages within a second and no second carries more than 30, and a 429 from
Telegram moves the delivery's `next_at` to the `retry_after` it gives without counting it as a failed attempt. The
webhook takes at most 5 updates per 10 seconds from one chat, in each api process, and drops the rest, answering none
of them. The summary of a burst of notices (more than 20 in 10 minutes as one message) is not built.

### Unlinking

A member unlinks on the web (`DELETE /v1/me/telegram`), which deletes the channel and the deliveries waiting for it.
Sending `/stop` to the bot does the same. Each is audited as `telegram.unlink`, with `by=web` or `by=bot`. When
Telegram answers 403 because the member blocked the bot, or 400 for a chat it no longer knows, the hub turns the
channel off and says so on the web; linking again turns it on. Losing the grant on a project stops that project's
notifications, not the channel; deleting the member deletes their channels.

### What never goes to Telegram

Telegram is a sink outside the hub, so the message holds as little as the owner needs to decide:

- not a decision's context, a proposal's summary, paths or evidence, a run's log or diff, a step's evidence, the plan's
  text or the terminal: the web button leads to them;
- for a project whose hub sink is cleared for the level `customer` or the location `domestic-only` (or that has no hub
  sink), the project's name, the kind of thing that waits and the link, nothing else: no question, title, option,
  context, diff or file name, and no answer button;
- no token, pairing code or link code, ever;
- no command beyond answering a decision or a proposal: cancelling, approving, dispatching and messages to the agent stay
  on the web and the command line.
