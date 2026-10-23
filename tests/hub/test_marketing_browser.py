"""The script of the browser adapter of the core plugin marketing (``evo_agents.hub.marketing_browser``), as criteria a15
and a21 of plan autonomous-oss-growth name it: it fills a form and presses a button with no model, stops at a captcha, a
lock or the login page with a screenshot, never retries, and opens nothing but the compose page and the page of the
post. No test starts a browser, calls a platform or holds a credential: ``FakePage`` plays a local site."""

from __future__ import annotations

import json
import re

import pytest

from evo_agents.hub import marketing_browser as mb
from evo_agents.hub.marketing_browser import PROFILES, Post, run_post, trim_session

PNG = b"\x89PNG fake"
VIBLO = PROFILES["viblo"].at("http://site.test")
POST = Post("viblo", "A title\nThe text of the post", None)


class FakePage:
    """A site as a dict of address -> what the page shows; a click moves to ``after`` (or stays)."""

    def __init__(self, pages: dict[str, dict], after: dict | None = None):
        self.pages, self.after = pages, after
        self.url, self.status, self.visited, self.filled, self.clicked = "about:blank", 0, [], {}, []
        self.current: dict = {}

    def _show(self, address: str, page: dict) -> None:
        self.url, self.current = page.get("url", address), page
        self.status = page.get("status", 200)

    def goto(self, url: str) -> None:
        self.visited.append(url)
        self._show(url, self.pages.get(url, {"text": "not found", "status": 404}))

    def text(self) -> str:
        return self.current.get("text", "")

    def has(self, selector: str) -> bool:
        return selector in self.current.get("has", ())

    def fill(self, selector: str, value: str) -> None:
        self.filled[selector] = value

    def click(self, selector: str) -> None:
        self.clicked.append(selector)
        if self.after is not None:
            self._show(self.url, self.after)

    def settle(self) -> None:
        pass

    def screenshot(self) -> bytes:
        return PNG


def site(**more) -> FakePage:
    compose = "http://site.test/publish/posts/new"
    return FakePage({compose: {"text": "New post"}}, {"url": "http://site.test/p/a-title-1", "text": "Done", **more})


def test_marketing_browser_script_fills_the_form_presses_the_button_and_reports_the_post_with_a_screenshot():
    page = site()
    done = run_post(page, VIBLO, POST)
    assert (done.outcome, done.url, done.screenshot) == ("posted", "http://site.test/p/a-title-1", PNG)
    assert page.filled == {'input[name="title"]': "A title", 'textarea[name="contents"]': "The text of the post"}
    assert page.clicked == ['button[type="submit"]']


def test_marketing_browser_script_touches_only_the_compose_page_and_never_reads_a_feed():
    for name in ("facebook_profile", "hackernews", "linkedin", "reddit"):
        profile = PROFILES[name].at("http://site.test")
        target = "python" if name == "reddit" else None
        page = FakePage({profile.compose_url(target): {"text": "ok"}}, {"url": profile.origin + "/x"})
        run_post(page, profile, Post(name, "line one\nline two", target))
        assert page.visited == [profile.compose_url(target)]  # one address: no feed, no comments, no numbers
    assert PROFILES["facebook_profile"].compose == "/"


@pytest.mark.parametrize(
    ("trouble", "outcome"),
    [
        ({"has": ('iframe[src*="recaptcha"]',)}, "captcha"),
        ({"has": ("#FunCaptcha",)}, "captcha"),
        ({"url": "http://site.test/checkpoint/1"}, "lockout"),
        ({"status": 429}, "lockout"),
        ({"status": 403}, "lockout"),
        ({"text": "Your account has been locked"}, "lockout"),
        ({"text": "Tài khoản của bạn đã bị khóa"}, "lockout"),
        ({"text": "We detected suspicious activity"}, "lockout"),
        ({"url": "http://site.test/login?next=/publish"}, "session_expired"),
        ({"has": ('input[type="password"]',)}, "session_expired"),
    ],
)
def test_marketing_browser_script_stops_on_a_captcha_a_lock_or_the_login_page_and_never_retries(trouble, outcome):
    compose = "http://site.test/publish/posts/new"
    page = FakePage({compose: {"text": "New post", **trouble}})
    done = run_post(page, VIBLO, POST)
    assert done.outcome == outcome and done.url is None and done.screenshot == PNG
    assert page.visited == [compose] and page.clicked == [] and page.filled == {}  # it stopped before it typed


def test_marketing_browser_script_stops_after_the_button_when_the_platform_answers_with_a_captcha():
    page = site(has=("[data-captcha]",), url="http://site.test/p/a-title-1")
    done = run_post(page, VIBLO, POST)
    assert done.outcome == "captcha" and len(page.clicked) == 1 and len(page.visited) == 1


def test_marketing_browser_script_calls_a_page_that_is_not_a_post_a_failure_and_a_locked_screenshot_still_returns():
    page = site(url="http://site.test/publish/posts/new")
    done = run_post(page, VIBLO, POST)
    assert done.outcome == "failed" and "not a post" in done.detail

    class Broken(FakePage):
        def screenshot(self) -> bytes:
            raise RuntimeError("no display")

    locked = Broken({"http://site.test/publish/posts/new": {"status": 429}})
    assert run_post(locked, VIBLO, POST).screenshot is None  # the outcome is kept without a picture


def test_marketing_browser_script_does_not_upload_a_video_for_tiktok():
    page = FakePage({})
    done = run_post(page, PROFILES["tiktok"], Post("tiktok", "caption", None))
    assert done.outcome == "failed" and "video" in done.detail and page.visited == []


def test_marketing_browser_script_has_no_model_and_no_playwright_import_in_its_logic():
    source = open(mb.__file__, encoding="utf-8").read()
    assert not re.search(r"^\s*(import|from)\s+(playwright|anthropic|openai|evo_agents\.worker)", source, re.M)
    assert "typing a password" not in source and "password" not in "".join(
        line for line in source.splitlines() if "fill(" in line
    )


def test_marketing_browser_session_is_cut_to_the_cookies_of_the_channel_and_fits_the_sealed_field():
    state = {
        "cookies": [
            {"name": "s", "value": "1", "domain": ".viblo.asia"},
            {"name": "t", "value": "2", "domain": "viblo.asia"},
            {"name": "o", "value": "3", "domain": ".other.example"},
            {"name": "x", "value": "4", "domain": "notviblo.asia"},
        ],
        "origins": [{"origin": "https://viblo.asia", "localStorage": []}],
    }
    kept = json.loads(trim_session(state, ("viblo.asia",)))
    assert [c["name"] for c in kept["cookies"]] == ["s", "t"] and kept["origins"]
    big = {**state, "origins": [{"origin": "https://viblo.asia", "localStorage": [{"name": "a", "value": "z" * 300}]}]}
    assert json.loads(trim_session(big, ("viblo.asia",), limit=300))["origins"] == []
    with pytest.raises(ValueError, match="log in again"):
        trim_session(state, ("viblo.asia",), limit=20)
