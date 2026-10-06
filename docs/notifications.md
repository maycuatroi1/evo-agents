# Notifications and decisions

A plan run (see [Plan runs](workers.md#plan-runs)) works for hours without anyone watching. Two things in it need
the member who dispatched it, the run's owner: a decision the agent may not take alone, and a push or merge the
owner should know about. The hub turns each into a notification for the owner and hands it to every channel the
owner has turned on. The web is the one channel of 0.4.0; Telegram is designed at the end of this page and comes in
a later plan, as a class added to the channel registry without a change of table or route.

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

The daemon sends `push_default_branch` itself for the pushes of `evo-agents worker step`; for a push or merge the
agent makes itself, the prompt tells it to run `evo-agents worker notify`. A run of one step never pushes a default
branch and sends no notice.

## Notifications, channels and deliveries

Each decision and each notice becomes, in the transaction that stores it, one notification for the run's owner with a
title, a body, a link to the web page that shows it, and the project, run and decision it belongs to; and one
delivery of it for each channel the owner has turned on. The table is the outbox: nothing is sent while the
transaction is open, and a hub that stops between the two loses nothing.

| Table | Columns |
| --- | --- |
| `notifications` | user, kind (`decision` or `notice`), project, run, decision, title, body, link, `created_at`, `read_at` |
| `notification_channels` | user, kind, `config` (JSON the channel's class reads), `enabled`, `created_at` |
| `notification_deliveries` | notification, channel, state (`pending`, `delivered`, `failed`), attempts, `next_at`, `last_error` |

Every member has the web channel without a row of `notification_channels`: a delivery without a channel is the
web's. A channel of another kind is a row, with what it needs in `config` (for Telegram, the chat it sends to).

The job `hub.deliver_notifications` runs every minute in the hub's worker, through the same procrastinate queue as
the other jobs (`docs/hub.md`). It takes the pending deliveries whose `next_at` has passed, with `FOR UPDATE SKIP
LOCKED`, and hands each to the class `CHANNELS` names for its channel's kind. The web's class marks a delivery
delivered at once, since the web reads notifications from the table. A class that fails leaves the delivery pending
with `last_error` and a later `next_at`, backing off, and the fifth failure (`MAX_DELIVERY_ATTEMPTS`) marks it
`failed`. A channel kind with no class in the registry fails its deliveries with that reason, so a hub that drops a
channel does not retry forever.

Adding a channel takes a class with one method, which sends one notification to one channel's `config` or raises,
and its entry in `CHANNELS`; the tables, the outbox and the job stay as they are. The routes that link a channel to
a member belong to that channel, like the Telegram ones below.

A notification is read when its owner opens it on the web, marks it read (`POST /v1/me/notifications/read`, by ids or
all), or answers its decision. The web shows the unread count on a bell in the top bar and the notifications in the
Inbox, open decisions first. The audit row `notification.read` names the notifications, not their text.

## Telegram, designed for a later plan

Nothing here is built in 0.4.0. It is the design the Telegram plan starts from, so that the tables, the registry and
the decision routes above already fit it.

### One bot, or one per member

This stays open until that plan (it is an open question of the plan that added this page):

- **One bot of the hub.** The hub admin creates it with BotFather and sets `EVO_HUB_TELEGRAM_BOT_TOKEN` and
  `EVO_HUB_TELEGRAM_WEBHOOK_SECRET`. Members only link their chat. One webhook, one place to rate limit, one token to
  rotate; the bot's name is the hub's.
- **A bot per member.** Each member brings a bot token, which the hub stores encrypted in the channel's `config`, and
  the hub registers a webhook per bot. No admin step, but the hub then holds many bot tokens, and each member deals
  with BotFather.

The webhook is reached either at a path of the hub's own domain (`/v1/telegram/webhook`, through the reverse proxy
that already sends `/v1` to the api) or at a subdomain of its own on Dokploy, which keeps Telegram's traffic apart
in the proxy's logs and limits. The first needs no change of the proxy; the second needs a domain and a route.

### Linking a chat

1. On the web, the member opens the notification settings and asks to link Telegram. The hub makes a one-time code
   of 32 random characters of `A-Z`, `a-z`, `0-9`, `_` and `-`, which a Telegram start parameter allows (64 at most),
   keeps its SHA-256 and the member, lets it live 10 minutes, and shows the link `https://t.me/<bot>?start=<code>`.
2. The member opens the link, and Telegram sends the bot `/start <code>` from that member's private chat.
3. The webhook finds the code by its hash. A used, expired or unknown code gets one reply that says only that the
   link did not work, whichever it was. A right code is used up, and the hub stores a channel of kind `telegram`
   whose `config` holds the chat id and the Telegram user id, and replies with the hub and the member it linked.
   Only a private chat links; a group gets the same reply as a wrong code.

A member has at most one Telegram channel; linking again replaces it.

### What a decision looks like there

The message names the project, the plan and the step, asks the question, and lists the options with their labels,
the recommended one marked. Under it, an inline keyboard has one button per option and a button that opens the
decision on the web. A button's `callback_data` (64 bytes at most) carries a short id of the decision and the option
key, never the text. The context is not sent: the web button leads to it.

### Answers

Telegram calls the webhook with the header `X-Telegram-Bot-Api-Secret-Token`, which the hub set as `secret_token`
when it called `setWebhook`. The hub compares it in constant time and drops anything else before it reads the body.
For a button, it looks up the channel by the Telegram user id and the chat, checks that its member owns the run and
that the decision is open, and answers through the same code as the web route (the inbox message, the audit row, the
run going on). It then calls `answerCallbackQuery`, so the button stops spinning, and edits the message to show the
answer, or that someone answered first. A reply to the decision's message is its answer in text; a member who wants
an option and text together answers on the web.

### Limits

Telegram's Bot FAQ asks bots to stay near one message per second in one chat and about 30 per second overall; the
plan checks the current figures before it builds. The job keeps under those, and a 429 from Telegram moves the
delivery's `next_at` to the `retry_after` it gives without counting it as a failed attempt. The hub also caps what
one member gets: notices beyond 20 in 10 minutes go out as one summary message with a link to the Inbox. The webhook
takes at most 5 updates per 10 seconds from one chat and drops the rest, answering none of them.

### Unlinking

A member unlinks on the web, which deletes the channel and fails its pending deliveries with the reason. Sending
`/stop` to the bot does the same. When Telegram answers 403 because the member blocked the bot, the hub turns the
channel off and says so on the web. Losing the grant on a project stops that project's notifications, not the
channel; deleting the member deletes their channels.

### What never goes to Telegram

Telegram is a sink outside the hub, so the message holds as little as the owner needs to decide:

- not a decision's context, a run's log or diff, a step's evidence, the plan's text or the terminal: the web button
  leads to them;
- nothing of a plan whose label is above what the Telegram sink is cleared for: such a notification says only that a
  decision or notice waits in the project, with the link;
- no token, pairing code or link code, ever;
- no command beyond answering a decision: cancelling, approving, dispatching and messages to the agent stay on the web
  and the command line.
