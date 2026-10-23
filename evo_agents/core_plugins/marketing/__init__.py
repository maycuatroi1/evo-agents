"""The core plugin marketing: the channel accounts a project posts on, and the gate every post passes before it goes out
(criteria a14, a10, a21 and a22 of plan autonomous-oss-growth; ``docs/marketing-plugin.md``).

A project registers accounts on github, hackernews, reddit, x, linkedin, viblo, facebook_page, facebook_profile and
tiktok (``registry``). Each has a kind (project or personal), a mode (autonomous or draft_only) and an adapter (api or
browser). The credentials are sealed in the plugin's config and never reach a lease, an environment or a prompt. A
personal account is made draft_only; only its owner switches facebook_profile or tiktok to autonomous, no agent changes
a mode, and a post command to a personal account that is draft_only, or on another channel, is refused by code.

Before any adapter a deterministic gate runs (``flow``): the deny-list of the project, the source rule, the caps, the
gap and the self-promotion rules of the channel (``gate``), a Judge in a fresh context (``judge``) and the monthly API
budget, 50 USD by default (``budget``). A post that passes goes out with no human; one that fails becomes a draft with
the reason. The audit of a post holds its channel, account, text, links, run, the result of each layer and its cost.
Each channel has a circuit breaker and the project a kill switch (``breaker``), with an Inbox and Telegram notice.

API adapters (``adapters``) post to GitHub (Releases, Discussions), X, LinkedIn, Reddit, Facebook Page and TikTok; the
browser adapter is a later step. The numbers of a post go to Signal at +24h and +7d (``metrics``, ``push``). The job
``advance`` (``job``) carries the posts the Judge answered later and reads the numbers. The routes under
``/v1/x/marketing/<project>`` (``server``, ``models``) and ``evo-agents hub marketing channels|posts|pause|resume|kill``
are their client. A project that does not enable the plugin writes nothing and answers 404 on its routes. The package
imports no other core plugin (import-linter contract ``marketing-plugin-own-package``).
"""

SERVER = "evo_agents.core_plugins.marketing.server"
JOB = "evo_agents.core_plugins.marketing.job"
ADVANCE_JOB = "advance"
ADVANCE_CRON = "*/5 * * * *"  # every 5 minutes, UTC
JUDGE = "marketing-judge"  # judge.AGENT_TYPE

JUDGE_INSTRUCTIONS = """# @marketing-judge

You judge one draft post of a project, in a fresh context: you do not know who wrote it or why, and you do not change
anything. You are asked a question that holds the draft, its channel and its links. Score four axes from 0 to 5.

- truth: every claim has a source in the draft's links (the docs, a release or the changelog).
- brand: it stays inside what the project says about itself.
- voice: no spam, no hype, no pressure to share or follow.
- privacy: no personal data of a third person, no private message, no name of someone who did not agree.

Answer with one JSON object and nothing else, for example {"truth": 5, "brand": 4, "voice": 4, "privacy": 5}. A draft
passes when every axis is 4 or more. When you doubt an axis, score it 3 or less."""

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
        "rules": {
            "title": "Rules of a channel",
            "description": "Per channel, the fields of its rules the project changes: posts_per_day, posts_per_week, "
            "min_gap_hours, max_links, max_chars, max_hashtags, self_promo_ratio.",
            "type": "object",
        },
        "github_token": {"title": "GitHub token", "description": "Releases and Discussions.", **SECRET},
        "x_token": {"title": "X token", "description": "A user access token that may post.", **SECRET},
        "linkedin_token": {"title": "LinkedIn token", "description": "An access token that may post.", **SECRET},
        "reddit_token": {"title": "Reddit token", "description": "An OAuth token, once Reddit approved it.", **SECRET},
        "facebook_page_token": {"title": "Facebook Page token", "description": "A Page access token.", **SECRET},
        "tiktok_token": {"title": "TikTok token", "description": "A Content Posting API token.", **SECRET},
    },
}

TABLES = (
    ("marketing_channels", "The channel accounts of a project: kind, mode, adapter and owner, no credential"),
    ("marketing_posts", "What was proposed to post, the result of each layer of the gate, its cost and where it ended"),
    ("marketing_budget", "The money reserved and spent in each month"),
    ("marketing_denylist", "The terms, domains and people no post may name"),
    ("marketing_state", "The kill switch of a project"),
    ("marketing_metrics", "The numbers of a post at +24h and +7d"),
)

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
            }
        ],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
    },
}

__all__ = ["MANIFEST"]
