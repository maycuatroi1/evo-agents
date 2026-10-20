"""The Telegram channel: the hub's one bot, a member's linked chat, the messages decisions, proposals and notices go
out as, and the answers that come back. ``model`` holds the bot's commands, its buttons and its messages,
``docs/notifications.md`` the design.

This is the server side of the core plugin telegram (``evo_agents.core_plugins.telegram``), which the hub reaches
through its manifest only, and which reaches the hub through the API of core plugins alone
(``evo_agents.hub.server.core_plugin_api``): ``TelegramChannel`` is its contribution to the channels, the three routers
its routes, which keep the paths they had before Telegram was a plugin.

The channel is on when EVO_HUB_TELEGRAM_BOT_TOKEN and EVO_HUB_TELEGRAM_WEBHOOK_SECRET are both set in the process's
environment, each of its shape (``settings``, read through ``hub.plugin_environment``); without them linking answers
503, the webhook 404, and a delivery to a Telegram channel fails at once, while everything else runs. The Bot API is
called with httpx (``BotApi``), never with the token in a log line, an error or the database.

A member links a chat from a web session, and from nothing else (a machine token gets 403, LINK_FROM_SESSION): POST
/v1/me/telegram/link makes a one-time code that lives 10 minutes (``hub.link_code``: only its SHA-256 is kept, with the
session that made it) and answers the link ``https://t.me/<bot>?start=<code>``; Telegram then sends the bot ``/start
<code>`` from the member's private chat, and the webhook links a channel of kind ``telegram`` whose config holds the
chat id, the Telegram user id and username, bound to that session (``hub.redeem_link_code``, ``hub.link_channel``). A
used, expired or unknown code, a code whose session ended, or a group chat, gets one reply that says only that the link
did not work. A member has one Telegram channel, and a chat links one member: linking again replaces both. GET
/v1/me/telegram says where the member's link stands; DELETE /v1/me/telegram unlinks (the channel and the deliveries
waiting for it go), and so does ``/stop`` in the chat, and a hub admin unlinks any member's chat (DELETE
/v1/admin/users/{login}/telegram, `evo-agents hub admin telegram-unlink LOGIN`). A message Telegram refuses with 403
(the member blocked the bot) turns the channel off, with the reason, until the member links again
(``hub.ChannelGone``).

The chat lives as long as the web session that linked it. Once that session is signed out, revoked (by its member or
a hub admin) or expired, the hub sends the chat nothing more (the delivery fails and the channel is turned off with
``hub.SESSION_ENDED``) and takes no answer from it, and the member links it again from a live session. A chat speaks
for its member with no token and never as a hub admin (``hub.Member``); what it may answer is what that member may
answer on the web, on the projects they hold a grant on now.

POST /v1/telegram/webhook takes Telegram's updates. It compares the header X-Telegram-Bot-Api-Secret-Token with the
secret in constant time before it reads the body (403 otherwise), takes at most INBOUND_UPDATES updates from one chat
in INBOUND_SECONDS and drops the rest unanswered, and answers 200 for anything it does not act on, so Telegram does not
send it again. A button answers a decision or a proposal (``hub.answer_decision``, ``hub.answer_proposal``) through the
same code as the web, as the member the chat is linked to, with no token (the audit row ends ``via=telegram``); then
the hub answers the button (answerCallbackQuery) and edits the message to say what happened, the web's button kept. A
reply to a decision's message is its answer in words. Before anything of a decision or proposal is read, the hub checks
that the member holds a grant on its project: a button or reply naming one they do not gets NOT_YOURS, the same words
whether it exists or not, and a project whose messages are restricted takes no answer from Telegram
(RESTRICTED_ANSWER), as its messages carry no buttons. A proposal takes no answer while a hub admin has turned the
feature curator off, as on the web.

``TelegramChannel`` is the class the plugin's manifest names for channels of kind ``telegram``. What a notification may
carry to a chat is the hub's to say (``hub.prepare`` with CLEARED, under customer and domestic-only on each project's
ladder); it sends the message ``model`` builds, at most one a second to one chat and 30 a second overall (Bot FAQ), and
answers the chat and Telegram's message_id (``model.message_ref``), kept as the delivery's external_id so a reply in
that chat finds its decision. A decision or proposal that no longer waits by then goes out as ``model.closed_message``,
never with its text. A 429 waits the retry_after Telegram gives without counting a failed try.

Hub admins see the bot and its webhook (GET /v1/admin/telegram) and register the webhook at the hub's public URL with
the secret (POST /v1/admin/telegram/webhook, audited ``telegram.webhook``): `evo-agents hub admin telegram
[--set-webhook]`; they unlink a member's chat with DELETE /v1/admin/users/{login}/telegram: `evo-agents hub admin
telegram-unlink LOGIN`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from evo_agents.hub.server import core_plugin_api as hub

from . import model as telegram

log = logging.getLogger(__name__)

KIND = "telegram"  # notification_channels.kind, and the audit rows' "via=telegram"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
WEBHOOK_PATH = "/v1/telegram/webhook"
ALLOWED_UPDATES = ["message", "callback_query"]
MAX_UPDATE_BYTES = 1024 * 1024
CALL_TIMEOUT_SECONDS = 10.0
WEBHOOK_SET = "telegram.webhook"  # a hub admin pointed the bot's webhook at the hub: the URL
TOKEN_VARIABLE, SECRET_VARIABLE = "EVO_HUB_TELEGRAM_BOT_TOKEN", "EVO_HUB_TELEGRAM_WEBHOOK_SECRET"
API_URL_VARIABLE = "EVO_HUB_TELEGRAM_API_URL"  # where the Bot API is, so tests can point the channel at a fake one
DEFAULT_API_URL = "https://api.telegram.org"
BOT_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}")  # <bot id>:<secret>, as BotFather gives it
WEBHOOK_SECRET = re.compile(r"[A-Za-z0-9_-]{1,256}")  # what setWebhook takes as secret_token
CLEARED = hub.Clearance("customer", "domestic-only", below=True)  # what may reach a chat, on each project's ladder
NOT_SET_UP = "Telegram is not set up on this hub: its admin sets EVO_HUB_TELEGRAM_BOT_TOKEN and _WEBHOOK_SECRET"
LINK_FROM_SESSION = (
    "a Telegram chat is linked from a web session only, so a token that leaked cannot link a chat of its own: sign in "
    "on the web and link it from the Inbox"
)
NOT_YOURS = "The hub has nothing by that for you to answer: it is not of a project you hold a grant on, or not there."
RESTRICTED_ANSWER = "Answer this on the hub: its project keeps its decisions and proposals off Telegram."
NOT_THE_HUBS = "This button is not the hub's."
NOT_LINKED = "This chat is not linked to the hub: link it from the hub's Inbox first."
NO_LONGER_OPEN = "This is no longer open: it was answered already, or it closed."
WORDS_FOR_DECISIONS = "Only a decision takes an answer in words: answer a proposal with its buttons."
TOO_LONG = "That answer is too long for the hub (4 KiB at most): answer on the web."
THE_HUBS_WORDS = {hub.NOT_YOURS: NOT_YOURS, hub.RESTRICTED_ANSWER: RESTRICTED_ANSWER}  # what this channel says instead

TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests put a fake Bot API here
pause = asyncio.sleep  # how the channel waits between messages; tests record it instead

me_router = APIRouter(prefix="/v1/me", tags=["telegram"], responses={401: {"model": hub.ErrorBody}})
webhook_router = APIRouter(prefix="/v1/telegram", tags=["telegram"])
admin_router = APIRouter(prefix="/v1/admin", tags=["telegram"], responses={401: {"model": hub.ErrorBody}})


def plugin() -> hub.Plugin:
    return hub.plugin(__name__)


# The bot's settings


@dataclass(frozen=True)
class Settings:
    """The bot's token, its webhook's secret and where the Bot API is; never logged or returned."""

    token: str | None
    secret: str | None
    api_url: str | None

    def missing(self) -> list[str]:
        """The variables the channel still needs; empty when it is on."""
        values = ((TOKEN_VARIABLE, self.token), (SECRET_VARIABLE, self.secret), (API_URL_VARIABLE, self.api_url))
        return [name for name, value in values if not value]

    @property
    def configured(self) -> bool:
        return not self.missing()


def _shaped(environment, name: str, shape: re.Pattern) -> str | None:
    """The value of variable ``name``, or None when it is unset or not of its shape, which is logged by name alone."""
    value = environment.get(name, "").strip() or None
    if value is not None and not shape.fullmatch(value):
        log.warning("telegram variable of the wrong shape, so the channel is off", extra={"variable": name})
        return None
    return value


def _api_url(environment) -> str | None:
    """EVO_HUB_TELEGRAM_API_URL as an http(s) base URL without a trailing slash, the Bot API's when unset; None for
    another value, which turns the channel off."""
    raw = environment.get(API_URL_VARIABLE, "").strip()
    if not raw:
        return DEFAULT_API_URL
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        log.warning("telegram variable of the wrong shape, so the channel is off", extra={"variable": API_URL_VARIABLE})
        return None
    return raw.rstrip("/")


def settings() -> Settings:
    """The EVO_HUB_TELEGRAM_* variables of this process, read now."""
    environment = hub.plugin_environment(plugin())
    return Settings(
        _shaped(environment, TOKEN_VARIABLE, BOT_TOKEN),
        _shaped(environment, SECRET_VARIABLE, WEBHOOK_SECRET),
        _api_url(environment),
    )


# The Bot API


class BotApiError(Exception):
    """Telegram refused a call, or could not be reached (``code`` None). Never holds the token."""

    def __init__(self, code: int | None, description: str, retry_after: float | None = None):
        super().__init__(f"Telegram answered {code}: {description}" if code else description)
        self.code = code
        self.description = description
        self.retry_after = retry_after


class BotApi:
    """The Bot API of the hub's bot. Each call takes a client of its own: the hub sends a few messages a minute."""

    def __init__(self, token: str, api_url: str):
        self._base = f"{api_url.rstrip('/')}/bot{token}/"

    @classmethod
    def from_settings(cls, found: Settings) -> BotApi | None:
        if not found.configured:
            return None
        return cls(found.token, found.api_url)

    async def call(self, method: str, **params) -> Any:
        payload = {key: value for key, value in params.items() if value is not None}
        try:
            async with httpx.AsyncClient(timeout=CALL_TIMEOUT_SECONDS, transport=TRANSPORT) as client:
                response = await client.post(self._base + method, json=payload)
        except httpx.HTTPError as exc:  # its text can hold the URL, and so the token: only its kind is kept
            raise BotApiError(None, f"Telegram could not be reached ({type(exc).__name__})") from None
        try:
            data = response.json()
        except ValueError:
            raise BotApiError(response.status_code, "Telegram answered without JSON") from None
        if isinstance(data, dict) and data.get("ok") is True:
            return data.get("result")
        found = data if isinstance(data, dict) else {}
        parameters = found.get("parameters") if isinstance(found.get("parameters"), dict) else {}
        retry = parameters.get("retry_after")
        description = " ".join(str(found.get("description") or "no description").split())[:300]
        code = found.get("error_code") if isinstance(found.get("error_code"), int) else response.status_code
        raise BotApiError(code, description, float(retry) if isinstance(retry, (int, float)) else None)


async def bot_username(app) -> str:
    """The bot's username, asked of Telegram once per process (getMe)."""
    known = getattr(app.state, "telegram_bot", None)
    if known:
        return known
    bot = BotApi.from_settings(settings())
    if bot is None:
        raise HTTPException(503, NOT_SET_UP)
    try:
        found = await bot.call("getMe")
    except BotApiError as exc:
        log.warning("telegram getMe failed", extra={"code": exc.code})
        raise HTTPException(502, f"Telegram did not say who the hub's bot is: {exc}") from None
    username = found.get("username") if isinstance(found, dict) else None
    if not isinstance(username, str) or not username:
        raise HTTPException(502, "Telegram did not say who the hub's bot is")
    app.state.telegram_bot = username
    return username


# The channel


class TelegramChannel(hub.Channel):
    """Sends notifications to members' Telegram chats, paced as the Bot FAQ asks."""

    needs_session = True  # a chat lives as long as the web session that linked it

    def __init__(self, config=None):
        super().__init__(config)
        self._last: dict[int, float] = {}
        self._recent: deque[float] = deque(maxlen=telegram.MESSAGES_PER_SECOND)

    async def prepare(self, conn, notification: hub.Outgoing) -> hub.Outgoing:
        # A decision or proposal always has a project; one that names none is restricted (fail closed).
        return await hub.prepare(conn, notification, CLEARED)

    def message(self, notification: hub.Outgoing) -> telegram.Message:
        url = hub.web_url(self.config, notification.link)
        extra = notification.extra or {}
        if extra.get("restricted", True):
            return telegram.minimal_message(
                notification.project, notification.kind, notification.notice_kind, url, notification.link
            )
        decision, proposal = extra.get("decision"), extra.get("proposal")
        if notification.kind == "decision":
            if not decision or decision["state"] != "open":  # answered or closed since: nothing of what it asked
                return telegram.closed_message(
                    notification.project, "decision", notification.decision_id, url, notification.link
                )
            return telegram.decision_message(
                decision_id=notification.decision_id,
                project=notification.project or "",
                run_id=notification.run_id,
                plan_id=decision["plan_id"],
                step_key=decision["step_key"],
                category=decision["category"],
                question=decision["question"],
                options=decision["options"] or [],
                url=url,
                link=notification.link,
                run_kind=decision.get("run_kind"),
                run_title=decision.get("run_title"),
            )
        if notification.kind == "proposal":
            if not proposal or proposal["state"] != "open":  # never its summary, whatever it became
                return telegram.closed_message(
                    notification.project, "proposal", notification.proposal_id, url, notification.link
                )
            return telegram.proposal_message(
                proposal_id=notification.proposal_id,
                project=notification.project or "",
                title=proposal["title"],
                tier=proposal["tier"],
                kind=proposal["kind"],
                lens=proposal["lens"],
                evidence=proposal["evidence_count"],
                url=url,
                link=notification.link,
            )
        return telegram.notice_message(
            project=notification.project,
            notice_kind=notification.notice_kind,
            title=notification.title,
            body=notification.body,
            url=url,
            link=notification.link,
        )

    async def _pace(self, chat_id: int) -> None:
        now = time.monotonic()
        wait = 0.0
        if chat_id in self._last:
            wait = self._last[chat_id] + telegram.CHAT_INTERVAL_SECONDS - now
        if len(self._recent) == self._recent.maxlen:
            wait = max(wait, self._recent[0] + 1.0 - now)
        if wait > 0:
            await pause(wait)
        stamp = time.monotonic()
        self._last[chat_id] = stamp
        self._recent.append(stamp)

    async def send(self, notification: hub.Outgoing, config: dict) -> str | None:
        bot = BotApi.from_settings(settings())
        if bot is None:
            raise hub.Undeliverable(NOT_SET_UP)
        chat_id = config.get("chat_id")
        if not isinstance(chat_id, int):
            raise hub.ChannelGone("the channel names no Telegram chat: link it again")
        message = self.message(notification)
        await self._pace(chat_id)
        try:
            sent = await bot.call(
                "sendMessage",
                chat_id=chat_id,
                text=message.text,
                parse_mode=telegram.PARSE_MODE,
                reply_markup=message.reply_markup(),
                link_preview_options={"is_disabled": True},
            )
        except BotApiError as exc:
            if exc.code == 429:
                raise hub.RetryAfter(exc.retry_after or 5, str(exc)) from None
            if exc.code == 403 or (exc.code == 400 and "chat not found" in exc.description.lower()):
                raise hub.ChannelGone(f"Telegram refused the chat ({exc.description}): link it again") from None
            raise
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        return telegram.message_ref(chat_id, message_id) if isinstance(message_id, int) else None


# Where a member's link stands


class TelegramStatus(BaseModel):
    configured: bool = Field(description="the hub has a bot: its admin set the EVO_HUB_TELEGRAM_* variables")
    linked: bool = Field(description="a chat of the member's is linked")
    enabled: bool = Field(description="the hub sends to it; false once Telegram refused it, as when the bot is blocked")
    username: str | None = Field(description="the Telegram username of the linked account, when it has one")
    linked_at: datetime | None
    disabled_reason: str | None = Field(description="why the hub stopped sending to the chat")
    bot: str | None = Field(description="the bot's username, once the hub asked Telegram for it")


class TelegramLink(BaseModel):
    url: str = Field(description="https://t.me/<bot>?start=<code>: open it in Telegram and press Start")
    bot: str
    expires_at: datetime = Field(description="10 minutes after it was made; it links one chat, once")


async def _status(conn, request: Request, user: hub.Session) -> TelegramStatus:
    found = await hub.linked_channel(conn, plugin(), user, KIND)
    username = None if found is None else found.config.get("username")
    return TelegramStatus(
        configured=settings().configured,
        linked=found is not None,
        enabled=found is not None and found.enabled,
        username=username if isinstance(username, str) else None,
        linked_at=None if found is None else found.linked_at,
        disabled_reason=None if found is None else found.disabled_reason,
        bot=getattr(request.app.state, "telegram_bot", None),
    )


@me_router.get("/telegram", response_model=TelegramStatus)
async def show(request: Request, user: hub.Caller) -> TelegramStatus:
    """Whether the hub has a bot, and whether a chat of the caller's is linked to it."""
    async with hub.begin(request) as conn:
        return await _status(conn, request, user)


@me_router.post(
    "/telegram/link",
    status_code=201,
    response_model=TelegramLink,
    responses={403: {"model": hub.ErrorBody}, 502: {"model": hub.ErrorBody}, 503: {"model": hub.ErrorBody}},
)
async def make_link(request: Request, user: hub.Caller) -> TelegramLink:
    """A one-time link that links the Telegram chat it is opened in to the caller, for 10 minutes; from a web session
    only, which the chat then lives no longer than. It replaces the caller's links not used yet."""
    if not settings().configured:
        raise HTTPException(503, NOT_SET_UP)
    if not user.web:
        raise HTTPException(403, LINK_FROM_SESSION)
    bot = await bot_username(request.app)
    async with hub.begin(request) as conn:
        made = await hub.link_code(conn, plugin(), user, KIND)
    log.info("telegram link made", extra={"login": user.login})
    return TelegramLink(url=telegram.link_url(bot, made.code), bot=bot, expires_at=made.expires_at)


@me_router.delete("/telegram", response_model=TelegramStatus)
async def unlink(request: Request, user: hub.Caller) -> TelegramStatus:
    """Unlink the caller's Telegram chat: the channel and the deliveries waiting for it go."""
    async with hub.begin(request) as conn:
        if await hub.unlink_channel(conn, plugin(), KIND, user.user_id, by="web"):
            log.info("telegram unlinked", extra={"login": user.login, "by": "web"})
        return await _status(conn, request, user)


# The webhook


class InboundLimit:
    """At most ``updates`` updates from one chat in ``seconds``, in this process."""

    MAX_CHATS = 10_000

    def __init__(self, updates: int = telegram.INBOUND_UPDATES, seconds: float = telegram.INBOUND_SECONDS):
        self.updates, self.seconds = updates, seconds
        self._seen: dict[int, deque[float]] = {}

    def allow(self, chat_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if len(self._seen) > self.MAX_CHATS:
            self._seen = {key: seen for key, seen in self._seen.items() if seen and now - seen[-1] < self.seconds}
        seen = self._seen.setdefault(chat_id, deque())
        while seen and now - seen[0] >= self.seconds:
            seen.popleft()
        if len(seen) >= self.updates:
            return False
        seen.append(now)
        return True


def inbound(app) -> InboundLimit:
    """The updates per chat the webhook takes in this process, made at the first update."""
    limit = getattr(app.state, "telegram_inbound", None)
    if limit is None:
        limit = app.state.telegram_inbound = InboundLimit()
    return limit


async def _member_of(conn, chat_id: int, sender_id: int, *, live: bool = True) -> hub.Member | None:
    """The member chat ``chat_id`` of Telegram user ``sender_id`` is linked to; with ``live``, only while the web
    session that linked it lives (an answer), without it whatever became of that session (``/stop``)."""
    return await hub.find_member(conn, plugin(), KIND, {"chat_id": chat_id, "user_id": sender_id}, live=live)


def _words(exc: HTTPException) -> str:
    """What the hub refused, in this channel's words when it has its own for them."""
    detail = exc.detail
    return THE_HUBS_WORDS.get(detail, str(detail)) if isinstance(detail, str) else str(detail)


LINK_FAILED = (
    "That link did not work: it was used, it expired, or it was not one of the hub's. Make a new one on the hub "
    "(Inbox, Telegram), and open it from your private chat with this bot."
)
HELP = (
    "This bot brings you the decisions, proposals and notices of Evo Agent Hub. Tap a button to answer, or "
    "reply to a decision's message with your answer in words. Link this chat from the hub (Inbox, Telegram); /stop "
    "unlinks it."
)


async def _link(conn, code: str, chat: dict, sender: dict) -> hub.Member | None:
    """Use link code ``code`` for ``chat``: the member it was made for, now linked, or None when it does not link."""
    if chat.get("type") != "private" or not isinstance(sender.get("id"), int):
        return None
    session = await hub.redeem_link_code(conn, plugin(), KIND, code)
    if session is None:
        return None
    config = {"chat_id": chat["id"], "user_id": sender["id"]}
    if isinstance(sender.get("username"), str):
        config["username"] = sender["username"][:64]
    # A chat speaks for one member: another member's link to it goes.
    return await hub.link_channel(conn, plugin(), session, KIND, config, unique_on=("chat_id",))


def _object(value) -> dict:
    """``value`` when Telegram sent an object there, else an empty one: an update is data, never trusted to be whole."""
    return value if isinstance(value, dict) else {}


class Updates:
    """What the webhook does with one update."""

    def __init__(self, request: Request, bot: BotApi):
        self.request = request
        self.app = request.app
        self.config = hub.hub_config(request)
        self.bot = bot

    async def say(self, chat_id: int, text: str, *, reply_to: int | None = None) -> None:
        try:
            await self.bot.call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
                link_preview_options={"is_disabled": True},
            )
        except BotApiError as exc:
            log.warning("telegram reply failed", extra={"code": exc.code})

    async def handle(self, update: dict) -> str:
        callback, message = update.get("callback_query"), update.get("message")
        if isinstance(callback, dict):
            chat_id = _object(_object(callback.get("message")).get("chat")).get("id")
        elif isinstance(message, dict):
            chat_id = _object(message.get("chat")).get("id")
        else:
            return "ignored"
        if not isinstance(chat_id, int):
            return "ignored"
        if not inbound(self.app).allow(chat_id):
            log.info("telegram update dropped: too many from one chat")
            return "dropped"
        if isinstance(callback, dict):
            return await self.button(callback, chat_id)
        return await self.text(message, chat_id)

    async def text(self, message: dict, chat_id: int) -> str:
        text = message.get("text") if isinstance(message.get("text"), str) else ""
        sender, chat = _object(message.get("from")), _object(message.get("chat"))
        found = telegram.command(text)
        if found is not None and found[0] == "start":
            return await self.start(chat_id, telegram.start_code(text), found[1], chat, sender)
        if found is not None and found[0] == "stop":
            return await self.stop(chat_id, sender)
        replied = message.get("reply_to_message")
        if found is None and text.strip() and isinstance(replied, dict) and isinstance(replied.get("message_id"), int):
            return await self.reply(message, chat_id, sender, replied["message_id"], text)
        await self.say(chat_id, HELP)
        return "help"

    async def start(self, chat_id: int, code: str | None, argument: str | None, chat: dict, sender: dict) -> str:
        """``/start``: help without an argument, else the link of ``code``."""
        if code is None and argument is None:
            await self.say(chat_id, HELP)
            return "help"
        member = None
        if code is not None:
            async with hub.begin(self.request) as conn:
                member = await _link(conn, code, chat, sender)
        if member is None:
            await self.say(chat_id, LINK_FAILED)
            return "link_failed"
        log.info("telegram linked", extra={"login": member.login})
        where = (hub.web_url(self.config, "/") or "").rstrip("/") or "Evo Agent Hub"
        await self.say(
            chat_id,
            f"Linked to {where} as {member.login}. Decisions, proposals and notices for you come here; /stop "
            "unlinks this chat.",
        )
        return "linked"

    async def stop(self, chat_id: int, sender: dict) -> str:
        """``/stop``: unlink the chat, whatever became of the web session that linked it."""
        sender_id = sender.get("id")
        member = None
        async with hub.begin(self.request) as conn:
            if isinstance(sender_id, int):
                member = await _member_of(conn, chat_id, sender_id, live=False)
            if member is not None:
                await hub.unlink_channel(conn, plugin(), KIND, member.user_id, by="bot")
        if member is not None:
            log.info("telegram unlinked", extra={"login": member.login, "by": "bot"})
        await self.say(chat_id, "This chat is unlinked from the hub: nothing more comes here until you link it again.")
        return "unlinked"

    async def reply(self, message: dict, chat_id: int, sender: dict, replied_id: int, text: str) -> str:
        """A reply to a message the hub sent in this chat: the answer in words to its decision."""
        sender_id = sender.get("id")
        try:
            async with hub.begin(self.request) as conn:
                member = await _member_of(conn, chat_id, sender_id) if isinstance(sender_id, int) else None
                if member is None:
                    outcome = NOT_LINKED
                else:
                    found = await hub.delivered_notification(conn, member, telegram.message_ref(chat_id, replied_id))
                    if found is None or found.kind != "decision" or found.decision_id is None:
                        outcome = WORDS_FOR_DECISIONS
                    else:
                        outcome = await self._answer_in_words(conn, member, found.decision_id, text)
        except HTTPException as exc:
            outcome = f"The hub did not take that answer: {_words(exc)}"
        await self.say(chat_id, outcome, reply_to=message.get("message_id"))
        return "replied"

    async def _answer_in_words(self, conn, member: hub.Member, decision_id: int, text: str) -> str:
        try:
            answered = await hub.answer_decision(conn, member, decision_id, CLEARED, text=text)
        except hub.Refused as exc:
            if exc.status_code == 422:  # the one body check words fail: their length
                return TOO_LONG
            raise
        return f"Your answer to decision #{decision_id} went to run #{answered.inbox_run}."

    async def button(self, callback: dict, chat_id: int) -> str:
        """A button of a decision's or a proposal's message."""
        data = telegram.parse_callback(callback.get("data"))
        sender_id = _object(callback.get("from")).get("id")
        message = _object(callback.get("message"))
        toast, settled, word = None, None, "refused"
        if data is None:
            toast = NOT_THE_HUBS
        else:
            try:
                async with hub.begin(self.request) as conn:
                    member = await _member_of(conn, chat_id, sender_id) if isinstance(sender_id, int) else None
                    if member is None:
                        toast = NOT_LINKED
                    else:
                        settled = await self._answer(conn, member, data)
                        toast, word = settled, "answered"
            except HTTPException as exc:
                toast = _words(exc)
                if exc.status_code == 409:
                    settled = NO_LONGER_OPEN
        await self.settle(callback, chat_id, message, toast, settled)
        return word

    async def settle(self, callback: dict, chat_id: int, message: dict, toast: str | None, settled: str | None) -> None:
        """Answer the button with ``toast``, and edit its message to end with ``settled``, its web button kept."""
        try:
            await self.bot.call(
                "answerCallbackQuery",
                callback_query_id=str(callback.get("id")),
                text=telegram.clip(toast or "Done.", telegram.TOAST_CHARS),
            )
            if settled is not None and isinstance(message.get("message_id"), int):
                buttons = telegram.url_buttons(message.get("reply_markup"))
                await self.bot.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=message["message_id"],
                    text=telegram.answered_text(str(message.get("text") or ""), settled),
                    parse_mode=telegram.PARSE_MODE,
                    reply_markup={"inline_keyboard": buttons} if buttons else None,
                    link_preview_options={"is_disabled": True},
                )
        except BotApiError as exc:
            log.warning("telegram answer to a button failed", extra={"code": exc.code})

    async def _answer(self, conn, member: hub.Member, data: telegram.Callback) -> str:
        """Answer what ``data`` names as ``member``, in ``conn``; what the message says it became. The hub checks the
        member's grant on its project and the project's clearance before it reads anything of it."""
        if data.kind == "decision":
            answered = await hub.answer_decision(conn, member, data.id, CLEARED, option=data.value)
            label = answered.option_label if answered.option_label is not None else data.value
            return f"Answered by {member.login}: {label}"
        proposal = await hub.answer_proposal(conn, member, data.id, CLEARED, data.value)
        return f"Proposal #{proposal.proposal_id} {proposal.state} by {member.login}"


class WebhookAnswer(BaseModel):
    ok: bool = True
    outcome: str = Field(description="what the hub did with the update")


@webhook_router.post(
    "/webhook",
    response_model=WebhookAnswer,
    responses={403: {"model": hub.ErrorBody}, 404: {"model": hub.ErrorBody}, 413: {"model": hub.ErrorBody}},
)
async def webhook(request: Request) -> WebhookAnswer:
    """Telegram's updates for the hub's bot, with the secret token setWebhook was given in the header
    X-Telegram-Bot-Api-Secret-Token."""
    found = settings()
    bot = BotApi.from_settings(found)
    if bot is None:
        raise HTTPException(404, NOT_SET_UP)
    given = request.headers.get(SECRET_HEADER, "")
    if not hmac.compare_digest(given.encode(), found.secret.encode()):
        log.warning("telegram update refused: the secret token is wrong")
        raise HTTPException(403, f"the header {SECRET_HEADER} does not hold this webhook's secret")
    raw = await request.body()
    if len(raw) > MAX_UPDATE_BYTES:
        raise HTTPException(413, "an update of Telegram is never this large")
    try:
        update = json.loads(raw)
    except ValueError:
        return WebhookAnswer(outcome="ignored")
    if not isinstance(update, dict):
        return WebhookAnswer(outcome="ignored")
    return WebhookAnswer(outcome=await Updates(request, bot).handle(update))


# The bot and its webhook, for hub admins


class TelegramWebhook(BaseModel):
    configured: bool = Field(description="the EVO_HUB_TELEGRAM_* variables are set")
    bot: str | None = Field(description="the bot's username")
    expected_url: str | None = Field(description="where the webhook should point: the hub's public URL")
    url: str | None = Field(description="where Telegram sends updates now; empty when no webhook is set")
    pending_update_count: int | None
    last_error_date: datetime | None
    last_error_message: str | None


def _expected(request: Request) -> str | None:
    return hub.web_url(hub.hub_config(request), WEBHOOK_PATH)


async def _webhook_view(request: Request) -> TelegramWebhook:
    bot = BotApi.from_settings(settings())
    if bot is None:
        return TelegramWebhook(
            configured=False,
            bot=None,
            expected_url=_expected(request),
            url=None,
            pending_update_count=None,
            last_error_date=None,
            last_error_message=None,
        )
    username = await bot_username(request.app)
    try:
        info = await bot.call("getWebhookInfo")
    except BotApiError as exc:
        raise HTTPException(502, f"Telegram did not say where its webhook is: {exc}") from None
    info = info if isinstance(info, dict) else {}
    when = info.get("last_error_date")
    return TelegramWebhook(
        configured=True,
        bot=username,
        expected_url=_expected(request),
        url=info.get("url") or None,
        pending_update_count=info.get("pending_update_count"),
        last_error_date=datetime.fromtimestamp(when, UTC) if isinstance(when, int) else None,
        last_error_message=info.get("last_error_message"),
    )


@admin_router.get("/telegram", response_model=TelegramWebhook, responses={403: {"model": hub.ErrorBody}})
async def show_webhook(request: Request, user: hub.HubAdmin) -> TelegramWebhook:
    """The hub's bot and where Telegram sends its updates."""
    return await _webhook_view(request)


@admin_router.post(
    "/telegram/webhook",
    response_model=TelegramWebhook,
    responses={
        403: {"model": hub.ErrorBody},
        409: {"model": hub.ErrorBody},
        502: {"model": hub.ErrorBody},
        503: {"model": hub.ErrorBody},
    },
)
async def set_webhook(request: Request, user: hub.HubAdmin) -> TelegramWebhook:
    """Point the bot's webhook at this hub (EVO_HUB_PUBLIC_URL + /v1/telegram/webhook) with the secret token."""
    found = settings()
    bot = BotApi.from_settings(found)
    if bot is None:
        raise HTTPException(503, NOT_SET_UP)
    url = _expected(request)
    if url is None:
        raise HTTPException(409, "the hub has no EVO_HUB_PUBLIC_URL, so Telegram has nowhere to send updates")
    try:
        await bot.call("setWebhook", url=url, secret_token=found.secret, allowed_updates=ALLOWED_UPDATES)
    except BotApiError as exc:
        raise HTTPException(502, f"Telegram did not set the webhook: {exc}") from None
    async with hub.begin(request) as conn:
        await hub.audit(conn, plugin(), user, WEBHOOK_SET, url)
    log.info("telegram webhook set", extra={"login": user.login})
    return await _webhook_view(request)


class TelegramUnlinked(BaseModel):
    login: str
    unlinked: bool = Field(description="a chat was linked to the member, and is not any more")


@admin_router.delete(
    "/users/{login}/telegram",
    response_model=TelegramUnlinked,
    responses={403: {"model": hub.ErrorBody}, 404: {"model": hub.ErrorBody}},
)
async def unlink_member(request: Request, login: hub.Login, user: hub.HubAdmin) -> TelegramUnlinked:
    """Unlink a member's Telegram chat, whatever its session: the channel and the deliveries waiting for it go."""
    async with hub.begin(request) as conn:
        done = await hub.unlink_by_login(conn, plugin(), KIND, login, actor=user)
    if done.unlinked:
        log.info("telegram unlinked", extra={"login": done.login, "by": "admin", "admin": user.login})
    return TelegramUnlinked(login=done.login, unlinked=done.unlinked)
