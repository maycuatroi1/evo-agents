"""The deterministic layers of the posting gate (``docs/marketing-plugin.md``): pure functions of the post, the
project's deny-list, the channel's rules and what the channel posted before. No network, no database, no model.

The order a post meets them in is fixed by ``flow``: the deny-list of the project, the source rule (every sentence about
evo-agents carries a link to the docs, a release or the changelog), the channel's caps, gap and self-promotion rule,
then the Judge (``judge``), then the budget (``budget``). A layer says whether the post passes and why in a short
sentence; the first that fails turns the post into a draft with that sentence as its reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from evo_agents.core_plugins.marketing.registry import Rules

URL = re.compile(r"https?://[^\s)>\]\"']+", re.IGNORECASE)
HASHTAG = re.compile(r"(?<!\w)#\w+")
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
SUBJECT = re.compile(r"evo[-\s]?agents?", re.IGNORECASE)  # the product the source rule is about
SOURCE_URL = re.compile(r"(/docs?(/|\b)|//docs\.|/releases?(/|\b)|/changelog|changelog\.md)", re.IGNORECASE)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = timedelta(days=7)


@dataclass(frozen=True)
class Layer:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail}


def _pass(name: str, detail: str = "") -> Layer:
    return Layer(name, True, detail)


def _fail(name: str, detail: str) -> Layer:
    return Layer(name, False, detail)


def urls_of(body: str, links: list[str]) -> list[str]:
    """Every link the post carries: the ones it lists and the ones in its text."""
    return [*links, *URL.findall(body)]


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url.lower()).split("/")[0].split(":")[0].removeprefix("www.")


# 1. The deny-list


def denylist(body: str, links: list[str], entries) -> Layer:
    """A term or a person the text names, or a domain a link points to, that the project's admins denied."""
    lowered = body.lower()
    hosts = {_host(url) for url in urls_of(body, links)}
    for entry in entries:
        value = entry.value.lower().strip()
        if entry.kind == "domain":
            domain = value.removeprefix("www.")
            if any(host == domain or host.endswith("." + domain) for host in hosts):
                return _fail("denylist", f"links to a denied domain ({entry.kind} #{entry.id})")
        elif value and value in lowered:
            return _fail("denylist", f"names a denied {entry.kind} (entry #{entry.id})")
    return _pass("denylist")


# 2. The source rule


def _sourced(sentence: str, sources: list[dict]) -> bool:
    if any(SOURCE_URL.search(url) for url in URL.findall(sentence)):
        return True
    for source in sources:
        quote, url = str(source.get("quote", "")).strip().lower(), str(source.get("url", ""))
        if quote and quote in sentence.lower() and SOURCE_URL.search(url):
            return True
    return False


def source_rule(body: str, sources: list[dict]) -> Layer:
    """Every sentence that speaks of evo-agents has a link to the docs, a release or the changelog: in the sentence, or
    in a source of the post that quotes it."""
    for sentence in (s.strip() for s in SENTENCE.split(body)):
        if sentence and SUBJECT.search(sentence) and not _sourced(sentence, sources):
            return _fail(
                "source",
                f"a sentence about evo-agents has no link to docs, a release or the changelog: {sentence[:80]!r}",
            )
    return _pass("source")


# 3. The channel's rules, caps and gap


def channel_rules(
    body: str,
    links: list[str],
    kind: str,
    rules: Rules,
    posted: list,
    now: datetime,
    *,
    promo: int = 0,
    replies: int = 0,
) -> Layer:
    """The size, links and hashtags the channel takes; for an original post also the caps a day and a week, the gap
    since the last one and the share of promotion. ``posted`` are the posts that went out in the last 30 days, oldest
    first; ``promo`` and ``replies`` count the original posts and the replies among them."""
    if len(body) > rules.max_chars:
        return _fail("rules", f"{len(body)} characters, the channel takes {rules.max_chars}")
    if len(urls_of(body, links)) > rules.max_links:
        return _fail("rules", f"more than {rules.max_links} links")
    if len(HASHTAG.findall(body)) > rules.max_hashtags:
        return _fail("rules", f"more than {rules.max_hashtags} hashtags")
    if kind == "reply":
        return _pass("rules")
    return _caps(rules, [p for p in posted if p.kind == "post"], now, promo, replies)


def _caps(rules: Rules, posted: list, now: datetime, promo: int, replies: int) -> Layer:
    if sum(1 for p in posted if now - p.posted_at < DAY) >= rules.posts_per_day:
        return _fail("rules", f"{rules.posts_per_day} post a day reached")
    if sum(1 for p in posted if now - p.posted_at < WEEK) >= rules.posts_per_week:
        return _fail("rules", f"{rules.posts_per_week} posts a week reached")
    if posted and now - posted[-1].posted_at < rules.min_gap_hours * HOUR:
        return _fail("rules", f"less than {rules.min_gap_hours:g} hours since the last post")
    if rules.self_promo_ratio < 1 and (promo + 1) > rules.self_promo_ratio * (promo + 1 + replies):
        return _fail("rules", f"self-promotion would pass {rules.self_promo_ratio:.0%} of what the account posts")
    return _pass("rules")
