"""The core plugin marketing: the channel accounts a project posts on, and the gate every post passes before it goes out
(criteria a14, a10, a21 and a22 of plan autonomous-oss-growth; ``docs/marketing-plugin.md``).

A project registers accounts on github, hackernews, reddit, x, linkedin, viblo, facebook_page, facebook_profile,
tiktok and facebook_group, whose posts are drafts a member shares by hand (``registry``). Each has a kind (project or personal), a mode (autonomous or draft_only) and an adapter (api or
browser). The credentials are sealed in the plugin's config and never reach a lease, an environment or a prompt. A
personal account is made draft_only; only its owner switches facebook_profile or tiktok to autonomous, no agent changes
a mode, and a post command to a personal account that is draft_only, or on another channel, is refused by code.

Before any adapter a deterministic gate runs (``flow``): the deny-list of the project, the source rule, the caps, the
gap and the self-promotion rules of the channel (``gate``), a Judge in a fresh context (``judge``) and the monthly API
budget, 50 USD by default (``budget``). A post that passes goes out with no human; one that fails becomes a draft with
the reason. The audit of a post holds its channel, account, text, links, run, the result of each layer and its cost.
Each channel has a circuit breaker and the project a kill switch (``breaker``), with an Inbox and Telegram notice.

API adapters (``adapters``) post to GitHub (Releases, Discussions), X, LinkedIn, Reddit, Facebook Page and TikTok. A
channel with no API (Viblo, the Facebook profile; Hacker News is draft only) posts through the browser adapter
(``browser``): a post that passes the gate is queued, and a deterministic script on a worker with a browser claims it,
fills the form with the logged-in session of the account and reports the link and a screenshot; a captcha, a lock or an
expired session stops the channel. The numbers of a post go to Signal at +24h and +7d (``metrics``, ``push``). The job
``advance`` (``job``) carries the posts the Judge answered later and reads the numbers. The routes under
``/v1/x/marketing/<project>`` (``server``, ``models``) and ``evo-agents hub marketing channels|posts|pause|resume|kill``
are their client. A project that does not enable the plugin writes nothing and answers 404 on its routes. The package
imports no other core plugin (import-linter contract ``marketing-plugin-own-package``).

On the Loop (criterion a16; ``loop``, ``campaign``, ``replies``, ``tools``) the agent type @marketer turns a claim of
the Channels or Customer segments cell that goes to testing into a plan of posts, each tied to its claim and a Signal
key and posted at its time through the gate; a post that went out is measured, and a reply under a project account's
post is answered within ``replies_per_day``. Content for a member's personal channel becomes a proposal for the hub
project the config names in ``proposals_project``.
"""

SERVER = "evo_agents.core_plugins.marketing.server"
JOB = "evo_agents.core_plugins.marketing.job"
ADVANCE_JOB = "advance"
ADVANCE_CRON = "*/5 * * * *"  # every 5 minutes, UTC
JUDGE = "marketing-judge"  # judge.AGENT_TYPE
MARKETER = "marketer"
LOOP = "evo_agents.core_plugins.marketing.loop"
TOOLS = "evo_agents.core_plugins.marketing.tools"

JUDGE_INSTRUCTIONS = """# @marketing-judge

You judge one draft post of a project, in a fresh context: you do not know who wrote it or why, and you do not change
anything. You are asked a question that holds the draft, its channel and its links. Score four axes from 0 to 5.

- truth: every claim has a source in the draft's links (the docs, a release or the changelog).
- brand: it stays inside what the project says about itself.
- voice: no spam, no hype, no pressure to share or follow.
- privacy: no personal data of a third person, no private message, no name of someone who did not agree.

Answer with one JSON object and nothing else, for example {"truth": 5, "brand": 4, "voice": 4, "privacy": 5}. A draft
passes when every axis is 4 or more. When you doubt an axis, score it 3 or less."""

MARKETER_INSTRUCTIONS = """# @marketer

You write the posts of an open-source project and take them through the posting gate. You never post by hand: every
post goes through a tool, and the gate decides.

## Tools

- marketing__propose: a post or a reply through the gate. Give the claim it tests (CL-<n>), one Signal key and, for
  a post that goes out later, scheduled_at.
- marketing__draft: a draft a person posts. Use it when the gate is not the right place, for example a launch.
- marketing__personal: content for a member's personal channel. Send the proposal it gives back with chat_message
  to the group chat growth-proposals of the hub project it names, as it is.

## Rules

- Language: English on github, hackernews, reddit, x and linkedin. Vietnamese on viblo, facebook_page,
  facebook_profile and tiktok.
- Every claim in a post has a source: the docs, a release or the changelog. Link it.
- Explain down to the mechanism: what the code does, step by step, and the numbers you measured, with the span.
- Write plainly. No hype, no superlatives, no call to share or follow, no emoji, no filler opening or closing line.
- Never reply from a personal account. Reply only from the project's accounts.
- You do not approve a baseline, and you do not change a Goal, the Policy or a channel's mode. When one of them
  should change, say so in your answer and stop.
- Post nothing that names a person who did not agree, a private message or someone's personal data.

## Proposals for the blog project

Follow dinh-vi.md of the blog project: one mechanism per post, measured numbers, Vietnamese. Fill each line of the
proposal: Đề xuất bài (the topic), Lý do (the Signal, its value and span), Kênh đích, Nguồn dùng được, Hạn.
"""

POST = {
    "channel_id": {"type": "integer", "description": "the account to post on (from the task's list)"},
    "body": {"type": "string", "description": "the text of the post"},
    "kind": {"type": "string", "enum": ["post", "reply"], "default": "post"},
    "reply_to": {"type": "string", "description": "for a reply: what it answers (issue#N, pr#N, discussion#N, a id)"},
    "links": {"type": "array", "items": {"type": "string"}},
    "sources": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"quote": {"type": "string"}, "url": {"type": "string"}},
            "required": ["quote", "url"],
        },
    },
    "claim": {"type": "string", "description": "the canvas claim the post tests, CL-<n>"},
    "signal_key": {"type": "string", "description": "the Signal key the post's numbers count toward"},
}
PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        **POST,
        "scheduled_at": {"type": "string", "format": "date-time", "description": "when it goes out; now if unset"},
    },
    "required": ["channel_id", "body"],
}
DRAFT_SCHEMA = {"type": "object", "properties": POST, "required": ["channel_id", "body"]}
PERSONAL_SCHEMA = {
    "type": "object",
    "properties": {
        **POST,
        "topic": {"type": "string", "description": "Chủ đề: the subject of the post, in Vietnamese"},
        "reason": {"type": "string", "description": "Lý do: the Signal, its value and the span it was measured over"},
        "deadline": {"type": "string", "description": "Hạn: the date the post is still timely until"},
    },
    "required": ["channel_id", "body", "topic", "reason", "deadline"],
}

SECRET = {"type": "string", "x-secret": True}
CONFIG = {
    "type": "object",
    "properties": {
        "budget_usd": {
            "title": "Monthly API budget (USD)",
            "description": "What the paid channels may cost in a calendar month; a paid channel stops until the next "
            "month when it is spent. Free channels are not stopped by it.",
            "type": "number",
            "minimum": 0,
            "default": 50,
        },
        "dry_run_hours": {
            "title": "Dry-run hours",
            "description": "How long after an account goes autonomous its posts that pass the gate stay drafts.",
            "type": "number",
            "minimum": 0,
            "default": 72,
        },
        "replies_per_day": {
            "title": "Replies a day",
            "description": "How many replies under the project accounts' posts the Loop gives @marketer to answer in "
            "24 hours.",
            "type": "integer",
            "minimum": 0,
            "default": 10,
        },
        "proposals_project": {
            "title": "Blog project",
            "description": "The hub project whose group chat growth-proposals gets the proposals for members' "
            "personal channels.",
            "type": "string",
            "default": "",
        },
        "rules": {
            "title": "Rules of a channel",
            "description": "Per channel, the fields of its rules the project changes: posts_per_day, posts_per_week, "
            "min_gap_hours, max_links, max_chars, max_hashtags, self_promo_ratio.",
            "type": "object",
        },
        "github_token": {"title": "GitHub token", "description": "Releases and Discussions.", **SECRET},
        "x_token": {"title": "X token", "description": "An OAuth 2.0 user access token that may post.", **SECRET},
        **{
            name: {
                "title": f"X OAuth 1.0a {title}",
                "description": "One of the four OAuth 1.0a user-context keys of the X account, from the app's Keys and "
                "tokens page; with all four set they sign the requests in place of x_token.",
                **SECRET,
            }
            for name, title in (
                ("x_consumer_key", "API key"),
                ("x_consumer_secret", "API key secret"),
                ("x_access_token", "access token"),
                ("x_access_token_secret", "access token secret"),
            )
        },
        "linkedin_token": {"title": "LinkedIn token", "description": "An access token that may post.", **SECRET},
        "reddit_token": {"title": "Reddit token", "description": "An OAuth token, once Reddit approved it.", **SECRET},
        "facebook_page_token": {"title": "Facebook Page token", "description": "A Page access token.", **SECRET},
        "tiktok_token": {"title": "TikTok token", "description": "A Content Posting API token.", **SECRET},
        **{
            f"{channel}_session": {
                "title": f"{name} browser session",
                "description": "The sealed login session of the account's browser, made once by `evo-agents hub "
                "marketing login`; only the worker's script that posts for the channel receives it.",
                **SECRET,
            }
            for channel, name in (
                ("hackernews", "Hacker News"),
                ("reddit", "Reddit"),
                ("linkedin", "LinkedIn"),
                ("viblo", "Viblo"),
                ("facebook_profile", "Facebook profile"),
                ("tiktok", "TikTok"),
            )
        },
    },
}

TABLES = (
    ("marketing_channels", "The channel accounts of a project: kind, mode, adapter and owner, no credential"),
    ("marketing_posts", "What was proposed to post, the result of each layer of the gate, its cost and where it ended"),
    ("marketing_budget", "The money reserved and spent in each month"),
    ("marketing_denylist", "The terms, domains and people no post may name"),
    ("marketing_state", "The kill switch of a project"),
    ("marketing_metrics", "The numbers of a post at +24h and +7d"),
    ("marketing_replies", "What someone answered under a post of a project account, once each"),
)

PAGES = {
    "overview": {
        "title": "Marketing",
        "icon": "message-square",
        "description": "Channels, breakers, budget, kill switch",
    },
    "posts": {"title": "Posts", "icon": "list-checks", "description": "Posts by the gate's result, and drafts to copy"},
    "weekly": {"title": "Growth weekly", "icon": "chart-column", "description": "Signal against Goal, posts and cost"},
}

MANIFEST = {
    "id": "marketing",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Marketing",
    "description": "Channel accounts and a posting gate: deny-list, source rule, caps, a Judge and a budget, with a "
    "breaker for each channel and a kill switch.",
    "engines": {"evo-agents": ">=0.21"},
    "plugin_api": 1,
    "permissions": [],
    "config": CONFIG,
    "contributes": {
        "routes": [{"name": "marketing", "title": "The channels and posts of a project", "python": f"{SERVER}:router"}],
        "pages": [{"name": name, **page} for name, page in PAGES.items()],
        "jobs": [
            {
                "name": ADVANCE_JOB,
                "title": "Carry posts through the gate, read their numbers, push them to Signal",
                "description": "Takes the posts the Judge answered later to the adapter, reads the numbers of posts "
                "at +24h and +7d, and pushes the project's numbers to Signal.",
                "python": f"{JOB}:run",
                "cron": ADVANCE_CRON,
            }
        ],
        "agent_types": [
            {
                "name": JUDGE,
                "title": "Marketing judge",
                "description": "Scores a draft post on truth, brand, voice and privacy, in a fresh context.",
                "mentioned_by": "admin",
                "profile": {
                    "handle": JUDGE,
                    "name": "Marketing judge",
                    "description": "Scores one draft post from 0 to 5 on four axes; changes nothing.",
                    "instructions": JUDGE_INSTRUCTIONS,
                },
            },
            {
                "name": MARKETER,
                "title": "Marketer",
                "description": "Turns a claim in testing into a plan of posts through the gate, answers replies on "
                "the project's accounts and proposes posts for members' personal channels.",
                "mentioned_by": "admin",
                "profile": {
                    "handle": MARKETER,
                    "name": "Marketer",
                    "description": "Writes the project's posts and takes them through the posting gate.",
                    "instructions": MARKETER_INSTRUCTIONS,
                },
            },
        ],
        "mcp_tools": [
            {
                "name": "propose",
                "title": "Propose a post through the gate",
                "description": "Take a post or a reply on a project account through the posting gate, now or at "
                "scheduled_at, tied to a canvas claim and a Signal key. Only a run of @marketer or @community calls it.",
                "python": f"{TOOLS}:propose",
                "input_schema": PROPOSE_SCHEMA,
            },
            {
                "name": "draft",
                "title": "Save a draft a person posts",
                "description": "Keep a draft that goes through no gate and posts nothing.",
                "python": f"{TOOLS}:draft",
                "input_schema": DRAFT_SCHEMA,
            },
            {
                "name": "personal",
                "title": "Propose content for a member's personal channel",
                "description": "Through the gate when its owner made the account autonomous on facebook_profile or "
                "tiktok, a draft otherwise; answers the proposal to send to the blog project.",
                "python": f"{TOOLS}:personal",
                "input_schema": PERSONAL_SCHEMA,
            },
        ],
        "loop_rules": [
            {
                "name": "growth",
                "python": f"{LOOP}:growth",
                "on": ["canvas.baseline"],
                "emits": [],
                "schedule": True,
                "kinds": [
                    {"kind": "growth_idea", "run": "task", "agent_type": MARKETER},
                    {"kind": "growth_draft", "run": "task", "agent_type": MARKETER},
                    {"kind": "growth_publish", "run": "job"},
                    {"kind": "growth_review", "run": "job"},
                ],
            }
        ],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
    },
}

__all__ = ["MANIFEST"]
