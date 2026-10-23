"""The parts of the plugin marketing that need no database: the channel registry, the deterministic layers of the gate
(deny-list, source rule, caps, gap, self-promotion), how the Judge's answer is read, and each API adapter against a
fake transport (no test calls a platform). The CLI commands are in the contract."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.core_plugins.marketing import adapters, gate, judge, registry
from evo_agents.core_plugins.marketing.adapters.base import AdapterError, Content, HttpResponse
from evo_agents.hub.contract import commands

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)
RELEASE = "https://github.com/maycuatroi1/evo-agents/releases/tag/v1"


def post(hours_ago: float, kind: str = "post"):
    return SimpleNamespace(kind=kind, posted_at=NOW - timedelta(hours=hours_ago))


def entry(kind: str, value: str, id: int = 1):
    return SimpleNamespace(kind=kind, value=value, id=id)


class Fake:
    """A transport that answers from a queue and keeps what it was sent."""

    def __init__(self, *answers: HttpResponse):
        self.answers, self.sent = list(answers), []

    async def send(self, request):
        self.sent.append(request)
        return self.answers.pop(0) if self.answers else HttpResponse(200, {})


def publish(channel: str, content: Content, fake: Fake, token: str = "tok-test"):
    return asyncio.run(adapters.for_channel(channel, fake).publish(content, token))


# The registry


def test_marketing_registry_names_the_ten_channels_and_the_kinds_each_takes():
    assert set(registry.CATALOG) == {
        "github", "hackernews", "reddit", "x", "linkedin", "viblo", "facebook_page", "facebook_profile", "tiktok",
        "facebook_group",
    }  # fmt: skip
    with pytest.raises(registry.RegistryError):
        registry.check_account("facebook_group", "project", None)
    assert registry.check_account("facebook_group", "personal", None) == "browser"
    assert not registry.may_be_autonomous("facebook_group", "personal")  # a member shares a group's draft by hand
    assert registry.check_account("github", "project", None) == "api"
    assert registry.check_account("viblo", "project", None) == "browser"
    with pytest.raises(registry.RegistryError):
        registry.check_account("facebook_profile", "project", None)
    with pytest.raises(registry.RegistryError):
        registry.check_account("github", "personal", None)


def test_marketing_registry_only_a_personal_facebook_profile_or_tiktok_may_go_autonomous():
    assert registry.may_be_autonomous("tiktok", "personal") and registry.may_be_autonomous(
        "facebook_profile", "personal"
    )
    assert not registry.may_be_autonomous("x", "personal") and not registry.may_be_autonomous("hackernews", "project")
    assert registry.may_be_autonomous("github", "project")


# The gate's deterministic layers


def test_marketing_gate_deny_list_stops_a_term_a_person_and_a_domain():
    layer = gate.denylist("Thanks to Dung for the idea", [], [entry("person", "dung")])
    assert not layer.ok and "denied person" in layer.detail
    assert not gate.denylist("see https://www.evil.example/x", [], [entry("domain", "evil.example")]).ok
    assert not gate.denylist("hi", ["https://sub.evil.example/"], [entry("domain", "evil.example")]).ok
    assert gate.denylist("hello world", [], [entry("term", "crypto"), entry("domain", "evil.example")]).ok


def test_marketing_gate_source_rule_wants_a_link_for_each_sentence_about_evo_agents():
    assert gate.source_rule(f"evo-agents 1.0 ships the loop {RELEASE}", []).ok
    assert gate.source_rule("Nothing about the product here.", []).ok
    bare = gate.source_rule("evo-agents is great.", [])
    assert not bare.ok and "evo-agents" in bare.detail
    second = gate.source_rule(f"evo-agents ships {RELEASE}.\nevo-agents is fast.", [])
    assert not second.ok  # one sourced sentence does not cover the next
    quoted = [{"quote": "evo-agents is fast", "url": "https://github.com/maycuatroi1/evo-agents/blob/main/docs/a.md"}]
    assert gate.source_rule("evo-agents is fast.", quoted).ok
    assert not gate.source_rule("evo-agents is fast.", [{"quote": "evo-agents is fast", "url": "https://x.example"}]).ok


def test_marketing_gate_channel_rules_hold_size_links_caps_and_gap():
    rules = registry.rules_for("x", {})
    assert not gate.channel_rules("a" * 281, [], "post", rules, [], NOW).ok
    assert not gate.channel_rules("hi #a #b #c", [], "post", rules, [], NOW).ok
    assert not gate.channel_rules(
        "hi", ["https://a.example", "https://b.example", "https://c.example"], "post", rules, [], NOW
    ).ok
    assert gate.channel_rules("hi", [], "post", rules, [], NOW).ok
    gap = gate.channel_rules("hi", [], "post", rules, [post(2)], NOW)
    assert not gap.ok and "hours since the last post" in gap.detail
    day = gate.channel_rules("hi", [], "post", rules, [post(30), post(20)], NOW)
    assert day.ok  # two a day on x, the first is a day old
    full = gate.channel_rules("hi", [], "post", rules, [post(30), post(10), post(6)], NOW)
    assert not full.ok
    assert gate.channel_rules("hi", [], "reply", rules, [post(1)], NOW).ok  # a reply is no original post


def test_marketing_gate_reddit_self_promotion_stays_under_its_share():
    rules = registry.rules_for("reddit", {"min_gap_hours": 0, "posts_per_week": 100, "posts_per_day": 100})
    first = gate.channel_rules("hi", [], "post", rules, [], NOW, promo=0, replies=0)
    assert not first.ok and "self-promotion" in first.detail
    assert gate.channel_rules("hi", [], "post", rules, [], NOW, promo=0, replies=9).ok
    assert not gate.channel_rules("hi", [], "post", rules, [], NOW, promo=1, replies=9).ok


def test_marketing_a_project_overrides_the_rules_of_a_channel_in_its_config():
    rules = registry.rules_for("x", {"x": {"posts_per_day": 5, "bogus": 1}})
    assert rules.posts_per_day == 5 and rules.max_chars == 280


# The Judge


def test_marketing_judge_passes_only_when_every_axis_is_four_or_more():
    ok = judge.parse('Here: {"truth": 5, "brand": 4, "voice": 4, "privacy": 5} done')
    assert ok.ok and not ok.near_line
    low = judge.parse('{"truth": 3, "brand": 5, "voice": 4, "privacy": 5}')
    assert not low.ok and low.near_line and "truth" in low.detail
    worse = judge.parse('{"truth": 3, "brand": 3, "voice": 4, "privacy": 5}')
    assert not worse.ok and not worse.near_line


def test_marketing_judge_an_answer_that_is_no_score_fails_closed():
    for text in ("looks good", "", '{"truth": 5}', '{"truth": 9, "brand": 5, "voice": 5, "privacy": 5}'):
        assert not judge.parse(text).ok
    assert "who proposed" not in judge.question("body", [], "x")


# The adapters, against a fake API


def test_marketing_adapter_github_posts_a_release_and_a_discussion():
    fake = Fake(HttpResponse(201, {"html_url": "https://github.com/a/b/releases/v1", "id": 7}))
    sent = publish("github", Content("Notes", "bot", "a/b", title="v1"), fake)
    assert sent.url.endswith("/releases/v1") and fake.sent[0].url == "https://api.github.com/repos/a/b/releases"
    assert fake.sent[0].json["tag_name"] == "v1" and fake.sent[0].headers["Authorization"] == "Bearer tok-test"
    graph = Fake(HttpResponse(200, {"data": {"createDiscussion": {"discussion": {"id": "D1", "url": "https://x/d"}}}}))
    found = publish("github", Content("Hi", "bot", "a/b#R1:C1", title="Hello"), graph)
    assert found.url == "https://x/d" and graph.sent[0].url.endswith("/graphql")
    assert graph.sent[0].json["variables"]["c"] == "C1"


def test_marketing_adapter_x_posts_a_tweet_a_reply_and_reads_its_numbers():
    fake = Fake(HttpResponse(201, {"data": {"id": "99"}}), HttpResponse(201, {"data": {"id": "100"}}))
    sent = publish("x", Content("hello", "evo", None), fake)
    assert sent.url == "https://x.com/evo/status/99" and sent.external_id == "99" and sent.cost_usd > 0
    publish("x", Content("answer", "evo", None, reply_to="55"), fake)
    assert fake.sent[1].json["reply"] == {"in_reply_to_tweet_id": "55"}
    numbers = Fake(HttpResponse(200, {"data": {"public_metrics": {"like_count": 3, "impression_count": 40}}}))
    assert asyncio.run(adapters.for_channel("x", numbers).metrics("99", "tok")) == {"likes": 3.0, "impressions": 40.0}


def test_marketing_adapter_linkedin_needs_an_author_urn_and_reads_the_post_id_header():
    with pytest.raises(AdapterError) as missing:
        publish("linkedin", Content("hi", "me", None), Fake())
    assert missing.value.kind == "missing"
    fake = Fake(HttpResponse(201, {}, {"x-restli-id": "urn:li:share:5"}))
    sent = publish("linkedin", Content("hi", "me", "urn:li:person:1"), fake)
    assert sent.external_id == "urn:li:share:5" and fake.sent[0].json["author"] == "urn:li:person:1"


def test_marketing_adapter_reddit_submits_comments_and_reads_the_score():
    fake = Fake(
        HttpResponse(200, {"json": {"errors": [], "data": {"url": "https://reddit.com/r/x/1", "name": "t3_1"}}}),
        HttpResponse(200, {"json": {"errors": [["RATELIMIT", "slow"]]}}),
    )
    sent = publish("reddit", Content("body", "me", "r/python", title="T"), fake)
    assert sent.external_id == "t3_1" and fake.sent[0].form["sr"] == "python" and fake.sent[0].form["kind"] == "self"
    with pytest.raises(AdapterError) as refused:
        publish("reddit", Content("body", "me", "r/python"), fake)
    assert refused.value.kind == "rejected"
    info = Fake(
        HttpResponse(
            200, {"data": {"children": [{"data": {"score": 0, "num_comments": 2, "removed_by_category": "moderator"}}]}}
        )
    )
    assert asyncio.run(adapters.for_channel("reddit", info).metrics("t3_1", "t")) == {
        "score": 0.0, "comments": 2.0, "removed": 1.0,
    }  # fmt: skip


def test_marketing_adapter_facebook_page_and_tiktok_post_and_never_read():
    page = Fake(HttpResponse(200, {"id": "1_2"}))
    assert publish("facebook_page", Content("hi", "page", "1"), page).external_id == "1_2"
    assert page.sent[0].url.endswith("/1/feed") and not hasattr(adapters.for_channel("facebook_page", page), "metrics")
    with pytest.raises(AdapterError) as bare:
        publish("tiktok", Content("hi", "me", None), Fake())
    assert bare.value.kind == "missing"
    clip = Fake(HttpResponse(200, {"data": {"publish_id": "p1"}}))
    sent = publish("tiktok", Content("hi", "me", None, media_url="https://x.example/a.png"), clip)
    assert sent.external_id == "p1" and clip.sent[0].json["source_info"]["photo_images"] == ["https://x.example/a.png"]
    assert not hasattr(adapters.for_channel("tiktok", clip), "metrics")


def test_marketing_adapter_errors_name_what_the_breaker_does_and_never_hold_the_credential():
    for status, kind in ((403, "locked"), (401, "locked"), (429, "failed"), (500, "failed"), (400, "rejected")):
        with pytest.raises(AdapterError) as failed:
            publish("x", Content("hi", "evo", None), Fake(HttpResponse(status, {})), token="tok-secret-value")
        assert failed.value.kind == kind and "tok-secret-value" not in failed.value.message
    with pytest.raises(AdapterError) as checkpoint:
        publish("x", Content("hi", "evo", None), Fake(HttpResponse(200 + 200, {"error": "checkpoint"})))
    assert checkpoint.value.kind == "locked"


def test_marketing_adapter_a_request_never_prints_its_credential():
    fake = Fake(HttpResponse(201, {"data": {"id": "1"}}))
    publish("x", Content("hi", "evo", None), fake, token="tok-secret-value")
    assert "tok-secret-value" not in repr(fake.sent[0])


def test_marketing_adapters_no_browser_adapter_yet_answers_none():
    assert adapters.for_channel("hackernews", Fake()) is None and adapters.for_channel("viblo", Fake()) is None


# The health check: a read that posts nothing


SECRET = "tok-secret-value"


def health(channel: str, target: str | None, *answers: HttpResponse) -> tuple[str, Fake]:
    fake = Fake(*answers)
    return asyncio.run(adapters.for_channel(channel, fake).health(target, SECRET)), fake


def refused(channel: str, target: str | None, *answers: HttpResponse) -> AdapterError:
    with pytest.raises(AdapterError) as failed:
        health(channel, target, *answers)
    assert SECRET not in failed.value.message
    return failed.value


def test_marketing_health_github_reads_the_target_repo_and_needs_push():
    said, fake = health("github", "a/b#R1:C1", HttpResponse(200, {"full_name": "a/b", "permissions": {"push": True}}))
    assert said == "push on a/b" and [(r.method, r.url) for r in fake.sent] == [
        ("GET", "https://api.github.com/repos/a/b")
    ]
    assert fake.sent[0].headers["Authorization"] == f"Bearer {SECRET}" and SECRET not in repr(fake.sent[0])
    assert "may not push" in refused("github", "a/b", HttpResponse(200, {"permissions": {"pull": True}})).message
    assert refused("github", None).kind == "missing"
    assert refused("github", "a/b", HttpResponse(404, {})).kind == "rejected"


def test_marketing_health_x_reddit_and_linkedin_read_whose_credential_it_is():
    said, fake = health("x", None, HttpResponse(200, {"data": {"id": "1", "username": "evo"}}))
    assert said == "signed in as @evo" and fake.sent[0].url == "https://api.x.com/2/users/me"
    assert refused("x", None, HttpResponse(401, {})).kind == "locked"
    said, fake = health("reddit", "r/python", HttpResponse(200, {"name": "evo"}))
    assert said == "signed in as u/evo" and fake.sent[0].url == "https://oauth.reddit.com/api/v1/me"
    assert refused("reddit", None, HttpResponse(200, {})).kind == "rejected"
    said, fake = health("linkedin", "urn:li:person:1", HttpResponse(200, {"sub": "1", "name": "Evo"}))
    assert "Evo" in said and fake.sent[0].url == "https://api.linkedin.com/v2/userinfo"
    assert refused("linkedin", None).kind == "missing"
    assert refused("linkedin", "urn:li:person:1", HttpResponse(403, {})).kind == "locked"
    assert all(r.method == "GET" for r in fake.sent)


def test_marketing_health_facebook_page_needs_create_content_and_tiktok_reads_the_creator():
    page = HttpResponse(200, {"name": "Evo", "tasks": ["ANALYZE", "CREATE_CONTENT"]})
    said, fake = health("facebook_page", "42", page)
    assert said == "may post on Evo" and fake.sent[0].url.endswith("/42?fields=name,tasks")
    assert fake.sent[0].method == "GET"
    assert "may not post" in refused("facebook_page", "42", HttpResponse(200, {"tasks": ["ANALYZE"]})).message
    assert refused("facebook_page", None).kind == "missing"
    creator = HttpResponse(200, {"data": {"creator_username": "evo"}, "error": {"code": "ok"}})
    said, fake = health("tiktok", None, creator)
    assert said == "posting as @evo" and fake.sent[0].url.endswith("/creator_info/query/")
    assert "publish/content" not in fake.sent[0].url  # the query, never the post
    bad = HttpResponse(200, {"data": {}, "error": {"code": "access_token_invalid"}})
    assert "access_token_invalid" in refused("tiktok", None, bad).message


def test_marketing_health_of_an_account_browser_draft_only_and_no_credential():
    from evo_agents.core_plugins.marketing import health as checks
    from evo_agents.core_plugins.marketing.settings import Config

    def row(channel, adapter="api", session="none", target=None):
        return SimpleNamespace(channel=channel, adapter=adapter, session_state=session, target=target)

    def check(cfg, chan, fake=None):
        return asyncio.run(checks.check(cfg, chan, fake or Fake()))

    bare, keyed = Config(), Config(tokens={"x": SECRET})
    assert check(bare, row("x")) == checks.Health("failing", "no credential")
    assert check(bare, row("hackernews", "browser")).state == "n/a"
    assert check(bare, row("facebook_group", "browser", "ok")).state == "n/a"
    assert check(bare, row("viblo", "browser", "ok")).state == "ok"
    assert check(bare, row("viblo", "browser", "stale")).state == "failing"
    assert check(Config(sessions={"reddit": "{}"}), row("reddit", session="ok")).state == "ok"  # posts by the browser
    assert check(bare, row("viblo")).state == "failing"  # no api adapter
    fine = check(keyed, row("x"), Fake(HttpResponse(200, {"data": {"id": "1", "username": "evo"}})))
    assert fine == checks.Health("ok", "signed in as @evo")
    locked = check(keyed, row("x"), Fake(HttpResponse(403, {})))
    assert locked.state == "failing" and SECRET not in locked.reason

    class Down:
        async def send(self, request):
            raise OSError(f"no route for {request.headers}")

    down = check(keyed, row("x"), Down())
    assert down == checks.Health("failing", "x: the platform did not answer (OSError)")


# The CLI contract


def test_marketing_cli_commands_are_in_the_contract():
    printed = commands()
    verbs = ("channels", "add", "check", "posts", "pause", "resume", "kill")
    assert {f"hub marketing {verb}" for verb in verbs} <= set(printed)
    assert printed["hub marketing channels"]["json"]["schema"] == "MarketingChannel"
    assert printed["hub marketing posts"]["json"]["kind"] == "array"
    options = {o["flags"][0]: o for o in printed["hub marketing kill"]["options"]}
    assert options["--project"]["required"] and "--json" in options
