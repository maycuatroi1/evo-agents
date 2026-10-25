"""The browser script of the CLI (``client`` browser_post of a plugin's command, ``evo_agents.hub.cli_plugin_commands``):
it posts one text a plugin's gate passed to a channel that has no posting API, in the logged-in browser session of the
account. The core plugin marketing is the one that uses it (``docs/marketing-plugin.md``, Browser adapter).

It is deterministic code and no model drives the browser: a ``Profile`` names the page, the fields and the button of one
channel, and ``run_post`` goes to the page, fills the text, presses the button and reads the address of the page it ends
on. After each step it looks for what a person would stop at: a captcha, a lock or checkpoint, a warning of the platform,
a 403 or 429, the login page. Any of them ends the run with the outcome ``captcha``, ``lockout`` or ``session_expired``
and a screenshot; the script never tries again, never solves a captcha and never types a password. It only posts: it
opens the compose page, the page of the post and nothing else, so it reads no feed, no comment and no number.

``Page`` is what the script needs of a browser; ``evo_agents.hub.browser_post_pw`` wraps Playwright in it, a test
passes a fake page that plays a local site. The selectors and addresses of ``PROFILES`` are [Unverified] against the live
sites. Standard library only: nothing here imports Playwright.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Protocol

OUTCOMES = ("posted", "captcha", "lockout", "session_expired", "failed")
MAX_SESSION_CHARS = 16 * 1024  # what a sealed field of the plugin's config holds
MAX_TITLE_CHARS = 120

CAPTCHA_SELECTORS = (
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="arkoselabs"]',
    ".g-recaptcha",
    ".h-captcha",
    "#FunCaptcha",
    "[data-captcha]",
)
LOCK_URL_PARTS = ("checkpoint", "challenge", "suspended", "locked", "captcha")
LOGIN_URL_PARTS = ("/login", "/signin", "/sign-in", "/sign_in", "/accounts/login", "/i/flow/login")
LOGIN_SELECTOR = 'input[type="password"]'
LOCK_PHRASES = (  # lower case; English and Vietnamese, the languages of the project's accounts
    "account has been locked",
    "account is locked",
    "temporarily restricted",
    "temporarily blocked",
    "suspicious activity",
    "unusual activity",
    "confirm your identity",
    "verify it's you",
    "we've restricted",
    "too many requests",
    "rate limit",
    "tài khoản của bạn đã bị khóa",
    "tài khoản của bạn đã bị khoá",
    "tạm thời bị khóa",
    "tạm khóa",
    "hoạt động bất thường",
    "xác nhận danh tính",
)


class Page(Protocol):
    """What the script asks of a browser page."""

    url: str
    status: int  # the HTTP status of the last page the browser loaded, 0 when unknown

    def goto(self, url: str) -> None: ...

    def text(self) -> str: ...

    def has(self, selector: str) -> bool: ...

    def fill(self, selector: str, value: str) -> None: ...

    def click(self, selector: str) -> None: ...

    def settle(self) -> None: ...

    def screenshot(self) -> bytes: ...


@dataclass(frozen=True)
class Profile:
    """The page of a channel a post is made on. ``compose`` may hold ``{target}`` (a subreddit)."""

    channel: str
    origin: str
    compose: str
    body: str  # selector of the field that takes the text
    submit: str  # selector of the button that posts
    done: str  # regular expression the address of the page after posting matches
    domains: tuple[str, ...]  # the cookies of these domains are the session
    login: str = "/login"  # where `evo-agents hub <plugin> login` opens the browser
    title: str | None = None  # selector of the title field, when the form has one
    needs_media: bool = False  # the form takes a file and no text alone

    def compose_url(self, target: str | None) -> str:
        return self.origin + self.compose.format(target=(target or "").strip("/"))

    def at(self, origin: str) -> Profile:
        """The same profile on another origin: a test's local site."""
        return replace(self, origin=origin, done=self.done.replace(re.escape(self.origin), re.escape(origin)))


PROFILES: dict[str, Profile] = {  # [Unverified] against the live sites: the member checks them at step 22
    "hackernews": Profile(
        "hackernews",
        "https://news.ycombinator.com",
        "/submit",
        'textarea[name="text"]',
        'input[type="submit"]',
        rf"{re.escape('https://news.ycombinator.com')}/item\?id=\d+",
        ("news.ycombinator.com",),
        login="/login",
        title='input[name="title"]',
    ),
    "reddit": Profile(
        "reddit",
        "https://www.reddit.com",
        "/{target}/submit",
        'textarea[name="text"]',
        'button[type="submit"]',
        rf"{re.escape('https://www.reddit.com')}/r/\w+/comments/\w+.*",
        ("reddit.com",),
        login="/login",
        title='textarea[name="title"]',
    ),
    "linkedin": Profile(
        "linkedin",
        "https://www.linkedin.com",
        "/feed/",
        'div[role="textbox"]',
        "button.share-actions__primary-action",
        rf"{re.escape('https://www.linkedin.com')}/(feed/update|posts)/.+",
        ("linkedin.com",),
        login="/login",
    ),
    "viblo": Profile(
        "viblo",
        "https://viblo.asia",
        "/publish/posts/new",
        'textarea[name="contents"]',
        'button[type="submit"]',
        rf"{re.escape('https://viblo.asia')}/p/[\w-]+",
        ("viblo.asia",),
        login="/login",
        title='input[name="title"]',
    ),
    "facebook_profile": Profile(
        "facebook_profile",
        "https://www.facebook.com",
        "/",
        'div[role="textbox"]',
        'div[aria-label="Post"]',
        rf"{re.escape('https://www.facebook.com')}/.*",
        ("facebook.com",),
        login="/login",
    ),
    "tiktok": Profile(
        "tiktok",
        "https://www.tiktok.com",
        "/upload",
        'div[contenteditable="true"]',
        'button[data-e2e="post_video_button"]',
        rf"{re.escape('https://www.tiktok.com')}/@[\w.]+/video/\d+",
        ("tiktok.com",),
        login="/login",
        needs_media=True,
    ),
}


@dataclass(frozen=True)
class Post:
    """What the script posts, from the claim of the plugin."""

    channel: str
    body: str
    target: str | None = None


@dataclass(frozen=True)
class Result:
    outcome: str  # one of OUTCOMES
    url: str | None = None  # the page of the post, when posted
    detail: str = ""
    screenshot: bytes | None = field(default=None, repr=False)


def _path(url: str) -> str:
    return re.sub(r"^[a-z]+://[^/]+", "", url.lower())


def detect(page: Page, profile: Profile) -> tuple[str, str] | None:
    """What stops the script on the page it is on: ``(outcome, detail)``, or None when nothing does. The checks use the
    page's address, its status, its text and its elements; no model reads the page."""
    for selector in CAPTCHA_SELECTORS:
        if page.has(selector):
            return "captcha", "the page asks for a captcha"
    path = _path(page.url)
    if any(part in path for part in LOCK_URL_PARTS):
        return "lockout", f"the platform redirected to a checkpoint or a challenge ({path.split('?')[0][:80]})"
    if page.status in (403, 429):
        return "lockout", f"the platform answered {page.status}"
    words = page.text().lower()
    for phrase in LOCK_PHRASES:
        if phrase in words:
            return "lockout", "the page warns that the account is locked, restricted or under review"
    if any(part in path.split("?")[0] for part in LOGIN_URL_PARTS) or page.has(LOGIN_SELECTOR):
        return "session_expired", f"the platform asks to log in again on {profile.channel}"
    return None


def title_and_text(body: str, titled: bool) -> tuple[str | None, str]:
    """A channel with a title field takes the first line as the title and the rest as the text."""
    if not titled:
        return None, body
    first, _, rest = body.strip().partition("\n")
    return first.strip()[:MAX_TITLE_CHARS], rest.strip() or first.strip()


def _shot(page: Page) -> bytes | None:
    try:
        return page.screenshot()
    except Exception:  # a page that cannot be photographed still ends the run with its outcome
        return None


def run_post(page: Page, profile: Profile, post: Post) -> Result:
    """Post ``post`` on ``page``: the compose page, the title, the text, the button, the page of the post. Ends at once on
    anything ``detect`` finds, with a screenshot, and never goes on."""

    def stopped(found: tuple[str, str]) -> Result:
        return Result(found[0], None, found[1], _shot(page))

    if profile.needs_media:
        return Result("failed", None, f"{profile.channel} takes a video, which the browser adapter does not upload")
    page.goto(profile.compose_url(post.target))
    page.settle()
    if found := detect(page, profile):
        return stopped(found)
    title, text = title_and_text(post.body, profile.title is not None)
    if profile.title and title:
        page.fill(profile.title, title)
    page.fill(profile.body, text)
    if found := detect(page, profile):
        return stopped(found)
    page.click(profile.submit)
    page.settle()
    if found := detect(page, profile):
        return stopped(found)
    if not re.fullmatch(profile.done, page.url):
        return Result("failed", None, f"the page after the button is {_path(page.url)[:80]}, not a post", _shot(page))
    return Result("posted", page.url, "", _shot(page))


def trim_session(state: str | dict, domains: tuple[str, ...], limit: int = MAX_SESSION_CHARS) -> str:
    """The login session (Playwright's storage state) as JSON of at most ``limit`` characters: first the cookies of the
    channel's domains alone, then without the local storage; ValueError when it still does not fit."""
    found = json.loads(state) if isinstance(state, str) else dict(state)
    cookies = [c for c in found.get("cookies", []) if _in_domains(str(c.get("domain", "")), domains)]
    kept = {"cookies": cookies, "origins": found.get("origins", [])}
    for candidate in (kept, {"cookies": cookies, "origins": []}):
        text = json.dumps(candidate, separators=(",", ":"), ensure_ascii=False)
        if len(text) <= limit:
            return text
    raise ValueError(
        f"the cookies of {', '.join(domains)} alone are over {limit} characters: log in again in a clean profile"
    )


def _in_domains(cookie: str, domains: tuple[str, ...]) -> bool:
    bare = cookie.lstrip(".").lower()
    return any(bare == d.lstrip(".") or bare.endswith("." + d.lstrip(".")) for d in domains)
