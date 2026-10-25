"""The channel registry of the plugin marketing: which channels exist, what kind of account each takes, how it is
reached by default and its default rules (``docs/marketing-plugin.md``).

Every number here is a default: a project overrides the rules of a channel in the plugin's config (``rules``). The
limits of each platform are [Unverified] against its current terms; ``docs/marketing-plugin.md`` says so and names the
page to check. Hacker News has no posting API and no account kind that may post by itself: it is draft_only always.
A Facebook group is never posted to by a machine: facebook_group takes a member's account, draft_only always.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from evo_agents.core_plugins.marketing.tables import ACCOUNT_KINDS, ADAPTERS, CHANNELS, PERSONAL_AUTONOMOUS

DEFAULT_BUDGET_USD = 50.0
DEFAULT_DRY_RUN_HOURS = 72  # a channel made autonomous only drafts for this long
DEFAULT_REPLIES_PER_DAY = 10  # reply work items of the Loop in 24 hours (plan autonomous-oss-growth, step 19)


@dataclass(frozen=True)
class Spec:
    channel: str
    kinds: tuple[str, ...]  # the account kinds the channel takes
    adapter: str  # how it is reached unless the account says otherwise
    cost_usd: float = 0.0  # what one post costs; above 0 the channel is paid and the budget stops it
    scored: bool = False  # posts get a score a breaker reads (Reddit)
    always_draft: bool = False  # no code path posts on it
    replies: bool = False  # the API takes a reply to someone else's post (never on a personal account)


@dataclass(frozen=True)
class Rules:
    posts_per_day: int = 1
    posts_per_week: int = 2
    min_gap_hours: float = 24.0
    max_links: int = 2
    max_chars: int = 3000
    max_hashtags: int = 3
    self_promo_ratio: float = 1.0  # at most this share of the last posts of the channel may promote the project


CATALOG: dict[str, Spec] = {
    "github": Spec("github", ("project",), "api"),
    "hackernews": Spec("hackernews", ("project", "personal"), "browser", always_draft=True),
    "reddit": Spec("reddit", ("project", "personal"), "api", scored=True),
    "x": Spec("x", ("project", "personal"), "api", cost_usd=0.01),
    "linkedin": Spec("linkedin", ("project", "personal"), "api"),
    "viblo": Spec("viblo", ("project", "personal"), "browser"),
    "facebook_page": Spec("facebook_page", ("project",), "api"),
    "facebook_profile": Spec("facebook_profile", ("personal",), "browser"),
    "tiktok": Spec("tiktok", ("project", "personal"), "api"),
    "facebook_group": Spec("facebook_group", ("personal",), "browser", always_draft=True),  # a member shares it
}

LIMITS: dict[str, dict] = {  # [Unverified] against each platform's current terms
    "github": {"max_chars": 10000},
    "hackernews": {"max_chars": 2000, "posts_per_week": 1},
    "reddit": {"max_chars": 10000, "posts_per_week": 1, "self_promo_ratio": 0.1},
    "x": {"max_chars": 280, "min_gap_hours": 4, "posts_per_day": 2, "posts_per_week": 5, "max_hashtags": 2},
    "linkedin": {"max_chars": 3000},
    "viblo": {"max_chars": 20000, "posts_per_week": 1},
    "facebook_page": {"max_chars": 5000},
    "facebook_profile": {"max_chars": 5000},
    "tiktok": {"max_chars": 2200},
    "facebook_group": {"max_chars": 5000},
}
RULE_FIELDS = frozenset(Rules.__dataclass_fields__)


class RegistryError(ValueError):
    """Why an account may not exist or change; the words go to the caller."""


def spec(channel: str) -> Spec:
    if channel not in CATALOG:
        raise RegistryError(f"{channel!r} is not a channel: use one of {', '.join(CHANNELS)}")
    return CATALOG[channel]


def check_account(channel: str, kind: str, adapter: str | None) -> str:
    """The adapter an account of ``channel`` and ``kind`` gets (the default when none is named); RegistryError when
    the channel does not take the kind or the adapter is none."""
    found = spec(channel)
    if kind not in ACCOUNT_KINDS or kind not in found.kinds:
        raise RegistryError(f"channel {channel} takes accounts of kind {' or '.join(found.kinds)}, not {kind!r}")
    chosen = adapter or found.adapter
    if chosen not in ADAPTERS:
        raise RegistryError(f"the adapter is api or browser, not {chosen!r}")
    return chosen


def may_be_autonomous(channel: str, kind: str) -> bool:
    """Whether an account may post without a human: not on a channel that is draft only, and a personal one only on
    the channels whose owner may switch it (facebook_profile and tiktok)."""
    found = spec(channel)
    if found.always_draft:
        return False
    return kind == "project" or channel in PERSONAL_AUTONOMOUS


def rules_for(channel: str, overrides: dict | None = None) -> Rules:
    """The rules of ``channel``: the defaults, the channel's limits, then the project's ``overrides`` of that channel
    (a value of the wrong type or a field that is none is left out)."""
    base = replace(Rules(), **LIMITS.get(channel, {}))
    picked = {}
    for name, value in ((overrides or {}).get(channel) or {}).items():
        want = type(getattr(base, name)) if name in RULE_FIELDS else None
        if want is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            picked[name] = want(value)
    return replace(base, **picked)
