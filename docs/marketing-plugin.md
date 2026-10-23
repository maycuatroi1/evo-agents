# The plugin marketing

Core plugin `marketing` (`evo_agents/core_plugins/marketing`, plan autonomous-oss-growth). A project that enables it
keeps channel accounts and posts through a gate. A project that does not enable it writes nothing, and its routes
answer 404.

## Channels

| Channel | Account kinds | Default adapter | Notes |
|---|---|---|---|
| github | project | api | Releases, and Discussions when the target is `owner/name#repoId:categoryId` |
| hackernews | project, personal | browser | draft_only always: no code path posts on it |
| reddit | project, personal | api | scored: a breaker reads the score; target `r/name` |
| x | project, personal | api | the only paid channel (0.01 USD a post, [Unverified]) |
| linkedin | project, personal | api | target is the author urn (`urn:li:person:...`) |
| viblo | project, personal | browser | posts through the browser adapter (below); first line is the title |
| facebook_page | project | api | target is the page id; posting only |
| facebook_profile | personal | browser | only its owner may switch it to autonomous; posts through the browser adapter |
| tiktok | project, personal | api | photo posts from an image link; posting only |

Each account has a kind (`project` or `personal`), a mode (`autonomous` or `draft_only`) and an adapter (`api` or
`browser`). Every account is made `draft_only`. An admin makes a project account; a writer makes a personal one, which
belongs to them.

## Modes and personal accounts

- Only an admin switches a project account to autonomous. Hacker News never goes autonomous.
- A personal account goes autonomous only for its owner, and only on `facebook_profile` and `tiktok`. An admin may take it
  back to `draft_only`.
- No request made with the scope of an agent's run changes a mode.
- A post command (`POST /posts`) to a personal account that is `draft_only`, on a channel outside those two, or a reply
  from a personal account, is refused with 403 by code. An agent leaves a text for a personal account with
  `POST /drafts`, which goes through no gate and posts nothing.
- Non-goals: no agent action on personal channels beyond original posts, no Facebook groups, no reading of Facebook or
  TikTok feeds or metrics, no paid ads, no scraping of people, no evasion of a captcha or a rate limit.

## Credentials

A credential is an `x-secret` field of the plugin's config (`github_token`, `x_token`, `linkedin_token`, `reddit_token`,
`facebook_page_token`, `tiktok_token`), sealed on the hub with its key and opened at the moment of a post. It goes into
one request header. It never goes into a lease, an environment, a prompt, a log or an answer of a route. The hub needs
`EVO_HUB_SECRETS_KEY` to store one.

Limitation: one credential for each channel type, shared by the accounts of that type in the project.

## The gate

A post (`POST /v1/x/marketing/{project}/posts`) meets the layers in this order and stops at the first that fails:

1. Switches: the kill switch of the project, the breaker of the channel and the mode of the account.
2. Deny-list: a term, a person or a domain the admins denied (the audit holds each change).
3. Source rule: every sentence about evo-agents has a link to the docs, a release or the changelog, in the sentence or in
   a `sources` entry that quotes it.
4. Rules of the channel: size, links, hashtags, posts a day and a week, the gap since the last post and, on Reddit, the
   share of self-promotion (replies count as non-promotion, and replies are exempt from caps and gap).
5. Judge in a fresh context (agent type `marketing-judge`, asked through the hub's question dispatch with the text and
   links only). It scores truth, brand, voice and privacy from 0 to 5. The post passes when every axis is 4 or more. An
   answer that does not read as a score fails the post. Exactly one axis at 3 also tells the admins in the Inbox.
6. Dry run: a channel made autonomous only drafts for `dry_run_hours` (default 72).
7. Budget: see below.

Then the adapter posts it. A post that passes needs no human. A post that fails becomes a `draft` with the reason; a post
the Judge fails is `withdrawn`. A post the Judge has not answered yet waits as `judging`, and the job `advance` (cron
`*/5 * * * *`) carries it on.

Default rules (a project changes them in the config field `rules`; every number is [Unverified] against the platform's
current terms, check each platform's policy page before relying on it): github 1 a day, 2 a week; reddit 1 a week,
self-promotion at most 10%; x 280 characters, 2 hashtags, 2 a day, 5 a week, 4 hours between posts; linkedin 3000
characters; tiktok 2200 characters; hackernews 1 a week.

## Breaker and kill switch

A breaker opens by itself and closes only by hand (an admin: `evo-agents hub marketing resume --project P CHANNEL_ID`):

- at once for a lock, a captcha, a checkpoint or a warning of the platform (status 401 or 403), and for a report on a post;
- after 2 failed posts in a row;
- after 2 posts the platform removed in 7 days;
- on Reddit, when 2 of the last 3 posts have a 24-hour score of zero or less;
- after 3 posts in a row that the Judge failed.

A channel whose breaker is open posts nothing: its posts are drafts. The kill switch stops every channel. Any member with a
grant sets it (`evo-agents hub marketing kill`), an admin clears it (`resume` without a channel id). It is set by itself
when 3 channels open their breaker within 24 hours, and when the month's spend passes the budget. Each opening, closing and
kill writes the audit and a notice of kind `plugin_notice` to the project's admins in the Inbox (and Telegram for those who
linked it).

## Budget

`budget_usd` in the config, default 50, a calendar month in UTC. A post on a paid channel reserves its expected cost before
it goes out and settles the real cost after. When spend plus reserve plus the new post would pass the budget, the post is a
draft and the paid channel stays stopped until the next month. Free channels are never stopped by it.

## Audit and numbers

Each post keeps its channel, account, content, links, the run that proposed it, the result of every layer, the Judge's
scores, the cost and its URL; each outcome writes an audit row (`marketing.post.posted`, `.draft`, `.withdrawn`,
`.failed`). The job reads the numbers of posts on X (likes, reposts, replies, impressions) and Reddit (score, comments,
removed) at +24h and +7d, and pushes them to Signal as `marketing.<metric>_24h` and `marketing.<metric>_7d`, with the
project numbers `marketing.posts_week`, `marketing.breaker_open`, `marketing.spend_usd_month` and
`marketing.judge_pass_rate`. A signal key must be declared by an admin first; an undeclared one is counted in `rejected`.
Posts are not label-gated: reading posts and the deny-list needs a writer, channels and status a reader.

## Commands

```
evo-agents hub marketing channels --project P [--json]
evo-agents hub marketing posts --project P [--state draft] [--json]
evo-agents hub marketing pause --project P CHANNEL_ID [--reason TEXT]
evo-agents hub marketing resume --project P [CHANNEL_ID]
evo-agents hub marketing kill --project P [--reason TEXT]
evo-agents hub marketing login --project P CHANNEL_ID
evo-agents hub marketing browser-post --project P [--json]
```

## Browser adapter

A channel with no posting API (Viblo, the Facebook profile; Reddit and LinkedIn while their API has no approved
credential; TikTok only as a draft, see below) posts through a deterministic script in a logged-in browser. No model
drives the browser: the script (`evo_agents/hub/marketing_browser.py`) opens the compose page, fills the text the gate
passed, presses the button and reads the address it lands on. It opens the compose page and the page of the post and
nothing else, so it reads no feed, no comment and no number; a browser post has no metrics.

The road of a post: the gate passes it, the post is `queued` (a draft with a reason when the account has no session, the
session is `stale`, another post of the channel is on its way, or the post is a reply). A script claims the oldest queued
post (`posting`), posts it, and reports the outcome with a screenshot; the post is then `posted` with its link, or
`failed`. The screenshot is kept on the post (`GET /v1/x/marketing/P/posts/ID/screenshot`) and its hash is in the post's
layers, labelled internal; the audit holds `marketing.post.posted` with the link.

The script stops, never retries, never solves a captcha and never types a password when it sees:

| Outcome | What it saw | What follows |
|---|---|---|
| `captcha` | a captcha frame or field | the post fails, the breaker of the channel opens at once |
| `lockout` | a checkpoint or challenge address, a 403 or 429, or a lock or restriction notice (English or Vietnamese) | the same |
| `session_expired` | the login page or a password field | the post fails, the channel is `stale` and its breaker opens, and the Inbox of the admins says its owner logs in again |
| `failed` | any other page after the button, or an error of the script | counts towards the two failures in a row that open the breaker |

A post that no script reports on in 30 minutes fails, and one that no script claims in a day becomes a draft. A claim
checks the kill switch, the breaker, the mode and the session again, as the queue may have waited.

### Log in once

The session is Playwright's storage state (the cookies of the channel's domains), sealed in the plugin's config field
`<channel>_session` (16 KiB at most; an admin reads no value back). The member who owns the account logs in once, on
a machine with a screen:

```
pip install playwright && playwright install chromium
evo-agents hub marketing login --project P CHANNEL_ID
```

The command opens a visible Chromium on the login page of the channel. Log in by hand (password, two-factor, any captcha),
press Enter in the terminal, and the command seals the session and marks the channel `ok`. Nothing types the password.
Log in again the same way when a session expires. The session reaches the hub-side claim of a person's login and nothing
else: the scope of an agent's run is refused, and the session is in no post, list, status or audit.

### The worker with a browser

`evo-agents hub marketing browser-post --project P` claims one queued post, posts it and reports. Run it, on a schedule,
on a machine or container that has Playwright and Chromium and the login of a member with the writer role (a container
from `mcr.microsoft.com/playwright/python` works, [Unverified]). It is a script of the member's login, not an agent run.
Two limits to know:

- The addresses and selectors of each channel (`PROFILES` in the script) are [Unverified] against the live sites; the
  tests use a fake page and never a real platform, so try a channel by hand, with a throwaway account, before relying on it.
- Hacker News stays a draft always. TikTok takes a video, which the script does not upload, so its posts stay drafts
  through the Content Posting API or by hand. LinkedIn's terms may forbid automation: the member decides, and the plugin
  does not post there through the browser until its owner stores a session.
