"""The Telegram channel: the hub's one bot, a member's linked chat, the messages decisions, proposals and notices go
out as, and the answers that come back. ``evo_agents.hub.telegram`` holds the model (codes, buttons, what may reach a
chat, the messages), ``docs/notifications.md`` the design.

The channel is on when EVO_HUB_TELEGRAM_BOT_TOKEN and EVO_HUB_TELEGRAM_WEBHOOK_SECRET are both set; without them
linking answers 503, the webhook 404, and a delivery to a Telegram channel fails at once, while everything else runs.
The Bot API is called with httpx (``BotApi``), never with the token in a log line, an error or the database.

A member links a chat from a web session, and from nothing else (a machine token gets 403, LINK_FROM_SESSION): POST
/v1/me/telegram/link makes a one-time code that lives 10 minutes (only its SHA-256 is kept, with the session that made
it) and answers the link ``https://t.me/<bot>?start=<code>``; Telegram then sends the bot ``/start <code>`` from the
member's private chat, and the webhook stores a channel of kind ``telegram`` whose config holds the chat id, the
Telegram user id and username, bound to that session (``token_id``). A used, expired or unknown code, a code whose
session ended, or a group chat, gets one reply that says only that the link did not work. A member has one Telegram
channel, and a chat links one member: linking again replaces both. GET /v1/me/telegram says where the member's link
stands; DELETE /v1/me/telegram unlinks (the channel and the deliveries waiting for it go), and so does ``/stop`` in the
chat, and a hub admin unlinks any member's chat (DELETE /v1/admin/users/{login}/telegram, `evo-agents hub admin
telegram-unlink LOGIN`). A message Telegram refuses with 403 (the member blocked the bot) turns the channel off, with
the reason, until the member links again (``notifications.ChannelGone``).

The chat lives as long as the web session that linked it. Once that session is signed out, revoked (by its member or
a hub admin) or expired, the hub sends the chat nothing more (the delivery fails and the channel is turned off with
``notifications.SESSION_ENDED``) and takes no answer from it, and the member links it again from a live session. A
chat speaks for its member with no token and never as a hub admin; what it may answer is what that member may answer
on the web, on the projects they hold a grant on now.

POST /v1/telegram/webhook takes Telegram's updates. It compares the header X-Telegram-Bot-Api-Secret-Token with the
secret in constant time before it reads the body (403 otherwise), takes at most INBOUND_UPDATES updates from one chat
in INBOUND_SECONDS and drops the rest unanswered, and answers 200 for anything it does not act on, so Telegram does not
send it again. A button answers a decision (``decisions.answer_decision``) or a proposal
(``proposals.answer_proposal_as``) through the same code as the web, as the member the chat is linked to, with no token
(the audit row ends ``via=telegram``); then the hub answers the button (answerCallbackQuery) and edits the message to
say what happened, the web's button kept. A reply to a decision's message is its answer in words. Before anything of a
decision or proposal is read, the hub checks that the member holds a grant on its project: a button or reply naming
one they do not gets NOT_YOURS, the same words whether it exists or not, and a project whose messages are restricted
takes no answer from Telegram (RESTRICTED_ANSWER), as its messages carry no buttons.

``TelegramChannel`` is the class CHANNELS names for kind ``telegram``. It reads the decision, the proposal, and the
project's ladder and hub sink of a notification (``prepare``), sends the message ``evo_agents.hub.telegram`` builds, at
most one a second to one chat and 30 a second overall (Bot FAQ), and answers the chat and Telegram's message_id
(``telegram.message_ref``), kept as the delivery's external_id so a reply in that chat finds its decision. A decision
or proposal that no longer waits by then goes out as ``telegram.closed_message``, never with its text. A 429 waits the
retry_after Telegram gives without counting a failed try.

Hub admins see the bot and its webhook (GET /v1/admin/telegram) and register the webhook at the hub's public URL with
the secret (POST /v1/admin/telegram/webhook): `evo-agents hub admin telegram [--set-webhook]`; they unlink a member's
chat with DELETE /v1/admin/users/{login}/telegram: `evo-agents hub admin telegram-unlink LOGIN`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, delete, exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import access, tables, telegram
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import Login
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import (
    CHANNELS,
    SESSION_ENDED,
    Channel,
    ChannelGone,
    Outgoing,
    RetryAfter,
    Undeliverable,
)
from evo_agents.hub.server.security import WEB, AdminUser, CurrentUser, Principal

log = logging.getLogger(__name__)

KIND = "telegram"  # notification_channels.kind
VIA = "telegram"  # the principal's kind, and the audit rows' "via=telegram"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
WEBHOOK_PATH = "/v1/telegram/webhook"
ALLOWED_UPDATES = ["message", "callback_query"]
MAX_UPDATE_BYTES = 1024 * 1024
CALL_TIMEOUT_SECONDS = 10.0
LINK = "telegram.link"  # a member linked a chat: "telegram user:<login>"
UNLINK = "telegram.unlink"  # and unlinked it: "telegram user:<login> by=<web|bot|admin>"
WEBHOOK_SET = "telegram.webhook"  # a hub admin pointed the bot's webhook at the hub: the URL
NOT_SET_UP = "Telegram is not set up on this hub: its admin sets EVO_HUB_TELEGRAM_BOT_TOKEN and _WEBHOOK_SECRET"
LINK_FROM_SESSION = (
    "a Telegram chat is linked from a web session only, so a token that leaked cannot link a chat of its own: sign in "
    "on the web and link it from the Inbox"
)
NOT_YOURS = "The hub has nothing by that for you to answer: it is not of a project you hold a grant on, or not there."
RESTRICTED_ANSWER = "Answer this on the hub: its project keeps its decisions and proposals off Telegram."
NOT_THE_HUBS = "This button is not the hub's."

TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests put a fake Bot API here
pause = asyncio.sleep  # how the channel waits between messages; tests record it instead

me_router = APIRouter(prefix="/v1/me", tags=["telegram"], responses={401: {"model": ErrorBody}})
webhook_router = APIRouter(prefix="/v1/telegram", tags=["telegram"])
admin_router = APIRouter(prefix="/v1/admin", tags=["telegram"], responses={401: {"model": ErrorBody}})


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
    def from_config(cls, config) -> BotApi | None:
        if config is None or config.telegram_missing():
            return None
        return cls(config.telegram_bot_token, config.telegram_api_url)

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
    bot = BotApi.from_config(app.state.config)
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


def _project_rules():
    """A project's name, ladder and hub sink's clearance (NULL without one): what ``telegram.restricted`` reads."""
    p, ps = tables.projects, tables.project_sinks
    hub_sink = and_(ps.c.project_id == p.c.id, ps.c.kind == access.HUB_KIND)
    return select(p.c.name, p.c.levels, p.c.locations, ps.c.clearance).select_from(p.outerjoin(ps, hub_sink))


def _restricted(row) -> bool:
    return row is None or telegram.restricted(row.clearance, row.levels, row.locations)


class TelegramChannel(Channel):
    """Sends notifications to members' Telegram chats, paced as the Bot FAQ asks."""

    needs_session = True  # a chat lives as long as the web session that linked it

    def __init__(self, config=None):
        super().__init__(config)
        self._last: dict[int, float] = {}
        self._recent: deque[float] = deque(maxlen=telegram.MESSAGES_PER_SECOND)

    async def prepare(self, conn: AsyncConnection, notification: Outgoing) -> Outgoing:
        # A decision or proposal always has a project; one that names none is restricted (fail closed).
        restricted = notification.kind in ("decision", "proposal")
        if notification.project is not None:
            query = _project_rules().where(tables.projects.c.name == notification.project)
            restricted = _restricted((await conn.execute(query)).one_or_none())
        extra: dict = {"restricted": restricted}
        if notification.decision_id is not None:
            d = tables.decisions
            query = select(d.c.question, d.c.category, d.c.plan_id, d.c.step_key, d.c.options, d.c.state)
            row = (await conn.execute(query.where(d.c.id == notification.decision_id))).one_or_none()
            extra["decision"] = None if row is None else dict(row._mapping)
        if notification.proposal_id is not None:
            p = tables.proposals
            query = select(p.c.title, p.c.tier, p.c.kind, p.c.lens, p.c.evidence_count, p.c.state)
            row = (await conn.execute(query.where(p.c.id == notification.proposal_id))).one_or_none()
            extra["proposal"] = None if row is None else dict(row._mapping)
        return dataclasses.replace(notification, extra=extra)

    def message(self, notification: Outgoing) -> telegram.Message:
        public_url = getattr(self.config, "public_url", None)
        url = telegram.web_url(public_url, notification.link)
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

    async def send(self, notification: Outgoing, config: dict) -> str | None:
        bot = BotApi.from_config(self.config)
        if bot is None:
            raise Undeliverable(NOT_SET_UP)
        chat_id = config.get("chat_id")
        if not isinstance(chat_id, int):
            raise ChannelGone("the channel names no Telegram chat: link it again")
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
                raise RetryAfter(exc.retry_after or 5, str(exc)) from None
            if exc.code == 403 or (exc.code == 400 and "chat not found" in exc.description.lower()):
                raise ChannelGone(f"Telegram refused the chat ({exc.description}): link it again") from None
            raise
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        return telegram.message_ref(chat_id, message_id) if isinstance(message_id, int) else None


CHANNELS[KIND] = TelegramChannel


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


def _live_session(channel_or_link):
    """The web session ``channel_or_link`` (a row of notification_channels or telegram_links) is bound to is live: of
    the same member, neither revoked nor expired."""
    t = tables.tokens
    return exists().where(
        t.c.id == channel_or_link.c.token_id,
        t.c.user_id == channel_or_link.c.user_id,
        t.c.kind == WEB,
        t.c.revoked_at.is_(None),
        t.c.expires_at > func.now(),
    )


def _channel_of(user_id: int):
    c = tables.notification_channels
    live = _live_session(c).label("session_live")
    return select(c.c.id, c.c.config, c.c.enabled, c.c.created_at, live).where(c.c.user_id == user_id, c.c.kind == KIND)


def _disabled_reason(row) -> str | None:
    if row is None or (row.enabled and row.session_live):
        return None
    if not row.enabled:
        return str(dict(row.config).get("disabled_reason") or "turned off")
    return SESSION_ENDED


async def _status(conn: AsyncConnection, request: Request, user_id: int) -> TelegramStatus:
    row = (await conn.execute(_channel_of(user_id))).one_or_none()
    config = dict(row.config) if row is not None else {}
    username = config.get("username")
    return TelegramStatus(
        configured=not request.app.state.config.telegram_missing(),
        linked=row is not None,
        enabled=bool(row is not None and row.enabled and row.session_live),
        username=username if isinstance(username, str) else None,
        linked_at=None if row is None else row.created_at,
        disabled_reason=_disabled_reason(row),
        bot=getattr(request.app.state, "telegram_bot", None),
    )


@me_router.get("/telegram", response_model=TelegramStatus)
async def show(request: Request, user: CurrentUser) -> TelegramStatus:
    """Whether the hub has a bot, and whether a chat of the caller's is linked to it."""
    async with request.app.state.engine.begin() as conn:
        return await _status(conn, request, user.user_id)


@me_router.post(
    "/telegram/link",
    status_code=201,
    response_model=TelegramLink,
    responses={403: {"model": ErrorBody}, 502: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def make_link(request: Request, user: CurrentUser) -> TelegramLink:
    """A one-time link that links the Telegram chat it is opened in to the caller, for 10 minutes; from a web session
    only, which the chat then lives no longer than. It replaces the caller's links not used yet."""
    if request.app.state.config.telegram_missing():
        raise HTTPException(503, NOT_SET_UP)
    if user.kind != WEB:
        raise HTTPException(403, LINK_FROM_SESSION)
    bot = await bot_username(request.app)
    code = telegram.new_code()
    links = tables.telegram_links
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            delete(links).where(
                (links.c.user_id == user.user_id) | (links.c.expires_at < func.now() - timedelta(days=1)),
                links.c.used_at.is_(None) | (links.c.used_at < func.now() - timedelta(days=1)),
            )
        )
        made = (
            insert(links)
            .values(
                user_id=user.user_id,
                code_hash=telegram.code_hash(code),
                expires_at=func.now() + timedelta(seconds=telegram.LINK_SECONDS),
                token_id=user.token_id,
            )
            .returning(links.c.expires_at)
        )
        expires_at = (await conn.execute(made)).scalar_one()
    log.info("telegram link made", extra={"login": user.login})
    return TelegramLink(url=telegram.link_url(bot, code), bot=bot, expires_at=expires_at)


async def _unlink(conn: AsyncConnection, user_id: int, login: str, by: str, *, actor: Principal | None = None) -> bool:
    """Delete member ``user_id``'s Telegram channel and the deliveries waiting for it, audited as done ``by`` the
    member (``web`` or ``bot``) or by ``actor``, a hub admin (``admin``)."""
    c = tables.notification_channels
    gone = (await conn.execute(delete(c).where(c.c.user_id == user_id, c.c.kind == KIND).returning(c.c.id))).all()
    if gone:
        await audit.record(
            conn,
            actor_id=user_id if actor is None else actor.user_id,
            token_id=None if actor is None else actor.token_id,
            action=UNLINK,
            target=f"telegram user:{login} by={by}",
        )
    return bool(gone)


@me_router.delete("/telegram", response_model=TelegramStatus)
async def unlink(request: Request, user: CurrentUser) -> TelegramStatus:
    """Unlink the caller's Telegram chat: the channel and the deliveries waiting for it go."""
    async with request.app.state.engine.begin() as conn:
        if await _unlink(conn, user.user_id, user.login, "web"):
            log.info("telegram unlinked", extra={"login": user.login, "by": "web"})
        return await _status(conn, request, user.user_id)


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


@dataclass(frozen=True)
class Member:
    user_id: int
    login: str


def _principal(member: Member) -> Principal:
    """The member a linked chat speaks for, as the answer routes check them: no token, of kind telegram, and never a
    hub admin, whoever the member is."""
    return Principal(
        user_id=member.user_id,
        login=member.login,
        admin=False,
        token_id=None,
        kind=VIA,
        token_hash="",
    )


async def _member_of(conn: AsyncConnection, chat_id: int, sender_id: int, *, live: bool = True) -> Member | None:
    """The member chat ``chat_id`` of Telegram user ``sender_id`` is linked to; with ``live``, only while the web
    session that linked it lives (an answer), without it whatever became of that session (``/stop``)."""
    c, u = tables.notification_channels, tables.users
    query = (
        select(u.c.id, u.c.login)
        .join_from(c, u, u.c.id == c.c.user_id)
        .where(c.c.kind == KIND, c.c.config.contains({"chat_id": chat_id, "user_id": sender_id}))
    )
    if live:
        query = query.where(_live_session(c))
    row = (await conn.execute(query.limit(1))).one_or_none()
    return None if row is None else Member(row.id, row.login)


def _answerable(kind: str, item_id: int, user_id: int):
    """The project of decision or proposal ``item_id`` with its ladder and hub sink, when member ``user_id`` holds a
    grant on it: no row for one of another project, as for none."""
    item = tables.decisions if kind == "decision" else tables.proposals
    p, g, ps = tables.projects, tables.grants, tables.project_sinks
    return (
        select(p.c.name, p.c.levels, p.c.locations, ps.c.clearance)
        .select_from(
            item.join(p, p.c.id == item.c.project_id)
            .join(g, and_(g.c.project_id == p.c.id, g.c.user_id == user_id))
            .outerjoin(ps, and_(ps.c.project_id == p.c.id, ps.c.kind == access.HUB_KIND))
        )
        .where(item.c.id == item_id)
    )


async def _answerable_project(conn: AsyncConnection, member: Member, kind: str, item_id: int) -> str:
    """The name of the project of what a button or a reply answers, once the member may answer it from Telegram:
    NOT_YOURS (404) without a grant on it, the same as for nothing by that id, and RESTRICTED_ANSWER (403) for a
    restricted project. Checked before anything of the decision or proposal is read."""
    row = (await conn.execute(_answerable(kind, item_id, member.user_id))).one_or_none()
    if row is None:
        raise HTTPException(404, NOT_YOURS)
    if _restricted(row):
        raise HTTPException(403, RESTRICTED_ANSWER)
    return row.name


LINK_FAILED = (
    "That link did not work: it was used, it expired, or it was not one of the hub's. Make a new one on the hub "
    "(Inbox, Telegram), and open it from your private chat with this bot."
)
HELP = (
    "This bot brings you the decisions, proposals and notices of the evo-agents hub. Tap a button to answer, or "
    "reply to a decision's message with your answer in words. Link this chat from the hub (Inbox, Telegram); /stop "
    "unlinks it."
)


async def _link(conn: AsyncConnection, code: str, chat: dict, sender: dict) -> Member | None:
    """Use link code ``code`` for ``chat``: the member it was made for, now linked, or None when it does not link."""
    if chat.get("type") != "private" or not isinstance(sender.get("id"), int):
        return None
    links, users, channels = tables.telegram_links, tables.users, tables.notification_channels
    query = (
        select(links.c.id, links.c.user_id, links.c.token_id, users.c.login)
        .join_from(links, users, users.c.id == links.c.user_id)
        .where(
            links.c.code_hash == telegram.code_hash(code),
            links.c.used_at.is_(None),
            links.c.expires_at > func.now(),
            _live_session(links),  # the web session that made the code still lives
        )
        .with_for_update(of=links)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    await conn.execute(update(links).values(used_at=func.now()).where(links.c.id == row.id))
    config = {"chat_id": chat["id"], "user_id": sender["id"]}
    if isinstance(sender.get("username"), str):
        config["username"] = sender["username"][:64]
    # A chat speaks for one member: another member's link to it goes.
    await conn.execute(
        delete(channels).where(
            channels.c.kind == KIND,
            channels.c.user_id != row.user_id,
            channels.c.config.contains({"chat_id": chat["id"]}),
        )
    )
    bound = {"config": config, "enabled": True, "token_id": row.token_id}
    linked = pg_insert(channels).values(user_id=row.user_id, kind=KIND, **bound)
    linked = linked.on_conflict_do_update(
        constraint="notification_channels_user_id_kind_key", set_={**bound, "created_at": func.now()}
    )
    await conn.execute(linked)
    await audit.record(conn, actor_id=row.user_id, token_id=None, action=LINK, target=f"telegram user:{row.login}")
    return Member(row.user_id, row.login)


def _object(value) -> dict:
    """``value`` when Telegram sent an object there, else an empty one: an update is data, never trusted to be whole."""
    return value if isinstance(value, dict) else {}


class Updates:
    """What the webhook does with one update."""

    def __init__(self, app):
        self.app = app
        self.config = app.state.config
        self.engine = app.state.engine
        self.bot = BotApi.from_config(self.config)

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
        if not self.app.state.telegram_inbound.allow(chat_id):
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
            code = telegram.start_code(text)
            if code is None and found[1] is None:
                await self.say(chat_id, HELP)
                return "help"
            member = None
            if code is not None:
                async with self.engine.begin() as conn:
                    member = await _link(conn, code, chat, sender)
            if member is None:
                await self.say(chat_id, LINK_FAILED)
                return "link_failed"
            log.info("telegram linked", extra={"login": member.login})
            hub = self.config.public_url or "the evo-agents hub"
            await self.say(
                chat_id,
                f"Linked to {hub} as {member.login}. Decisions, proposals and notices for you come here; /stop "
                "unlinks this chat.",
            )
            return "linked"
        if found is not None and found[0] == "stop":
            sender_id = sender.get("id")
            async with self.engine.begin() as conn:
                found = isinstance(sender_id, int)
                member = await _member_of(conn, chat_id, sender_id, live=False) if found else None
                if member is not None:
                    await _unlink(conn, member.user_id, member.login, "bot")
            if member is not None:
                log.info("telegram unlinked", extra={"login": member.login, "by": "bot"})
            await self.say(
                chat_id, "This chat is unlinked from the hub: nothing more comes here until you link it again."
            )
            return "unlinked"
        replied = message.get("reply_to_message")
        if found is None and text.strip() and isinstance(replied, dict) and isinstance(replied.get("message_id"), int):
            return await self.reply(message, chat_id, sender, replied["message_id"], text)
        await self.say(chat_id, HELP)
        return "help"

    async def reply(self, message: dict, chat_id: int, sender: dict, replied_id: int, text: str) -> str:
        """A reply to a message the hub sent in this chat: the answer in words to its decision."""
        n, dl, c = tables.notifications, tables.notification_deliveries, tables.notification_channels
        sender_id = sender.get("id")
        outcome = None
        try:
            async with self.engine.begin() as conn:
                member = await _member_of(conn, chat_id, sender_id) if isinstance(sender_id, int) else None
                if member is None:
                    outcome = "This chat is not linked to the hub: link it from the hub's Inbox first."
                else:
                    query = (
                        select(n.c.kind, n.c.decision_id)
                        .select_from(dl.join(n, n.c.id == dl.c.notification_id).join(c, c.c.id == dl.c.channel_id))
                        .where(
                            c.c.kind == KIND,
                            c.c.user_id == member.user_id,
                            dl.c.external_id == telegram.message_ref(chat_id, replied_id),
                            n.c.user_id == member.user_id,
                        )
                    )
                    row = (await conn.execute(query.limit(1))).one_or_none()
                    if row is None or row.kind != "decision" or row.decision_id is None:
                        outcome = "Only a decision takes an answer in words: answer a proposal with its buttons."
                    else:
                        outcome = await self._answer_in_words(conn, member, row.decision_id, text)
        except HTTPException as exc:
            outcome = f"The hub did not take that answer: {exc.detail}"
        await self.say(chat_id, outcome, reply_to=message.get("message_id"))
        return "replied"

    async def _answer_in_words(self, conn: AsyncConnection, member: Member, decision_id: int, text: str) -> str:
        from evo_agents.hub.server.decisions import AnswerIn, answer_decision

        project = await _answerable_project(conn, member, "decision", decision_id)
        try:
            body = AnswerIn(text=text)
        except ValidationError:
            return "That answer is too long for the hub (4 KiB at most): answer on the web."
        answered = await answer_decision(conn, _principal(member), project, decision_id, body, via=VIA)
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
                async with self.engine.begin() as conn:
                    member = await _member_of(conn, chat_id, sender_id) if isinstance(sender_id, int) else None
                    if member is None:
                        toast = "This chat is not linked to the hub: link it from the hub's Inbox first."
                    else:
                        settled = await self._answer(conn, member, data)
                        toast, word = settled, "answered"
            except HTTPException as exc:
                toast = str(exc.detail)
                if exc.status_code == 409:
                    settled = "This is no longer open: it was answered already, or it closed."
            except ValidationError:  # data the hub's buttons never carry, though it parsed
                toast = NOT_THE_HUBS
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
        return word

    async def _answer(self, conn: AsyncConnection, member: Member, data: telegram.Callback) -> str:
        """Answer what ``data`` names as ``member``, in ``conn``, once ``_answerable_project`` let it through; what the
        message says it became."""
        principal = _principal(member)
        project = await _answerable_project(conn, member, data.kind, data.id)
        if data.kind == "decision":
            from evo_agents.hub.server.decisions import AnswerIn, answer_decision

            answered = await answer_decision(conn, principal, project, data.id, AnswerIn(option=data.value), via=VIA)
            chosen = next((option for option in answered.decision.options if option.key == data.value), None)
            label = chosen.label if chosen else data.value
            return f"Answered by {member.login}: {label}"
        from evo_agents.hub.server.proposals import ProposalAnswer, answer_proposal_as

        proposal = await answer_proposal_as(
            conn, principal, project, data.id, ProposalAnswer(action=data.value), via=VIA
        )
        return f"Proposal #{proposal.id} {proposal.state} by {member.login}"


class WebhookAnswer(BaseModel):
    ok: bool = True
    outcome: str = Field(description="what the hub did with the update")


@webhook_router.post(
    "/webhook",
    response_model=WebhookAnswer,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 413: {"model": ErrorBody}},
)
async def webhook(request: Request) -> WebhookAnswer:
    """Telegram's updates for the hub's bot, with the secret token setWebhook was given in the header
    X-Telegram-Bot-Api-Secret-Token."""
    config = request.app.state.config
    if config.telegram_missing():
        raise HTTPException(404, NOT_SET_UP)
    given = request.headers.get(SECRET_HEADER, "")
    if not hmac.compare_digest(given.encode(), config.telegram_webhook_secret.encode()):
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
    return WebhookAnswer(outcome=await Updates(request.app).handle(update))


# The bot and its webhook, for hub admins


class TelegramWebhook(BaseModel):
    configured: bool = Field(description="the EVO_HUB_TELEGRAM_* variables are set")
    bot: str | None = Field(description="the bot's username")
    expected_url: str | None = Field(description="where the webhook should point: the hub's public URL")
    url: str | None = Field(description="where Telegram sends updates now; empty when no webhook is set")
    pending_update_count: int | None
    last_error_date: datetime | None
    last_error_message: str | None


def _expected(config) -> str | None:
    return f"{config.public_url}{WEBHOOK_PATH}" if config.public_url else None


async def _webhook_view(request: Request) -> TelegramWebhook:
    config = request.app.state.config
    if config.telegram_missing():
        return TelegramWebhook(
            configured=False,
            bot=None,
            expected_url=_expected(config),
            url=None,
            pending_update_count=None,
            last_error_date=None,
            last_error_message=None,
        )
    bot = await bot_username(request.app)
    try:
        info = await BotApi.from_config(config).call("getWebhookInfo")
    except BotApiError as exc:
        raise HTTPException(502, f"Telegram did not say where its webhook is: {exc}") from None
    info = info if isinstance(info, dict) else {}
    when = info.get("last_error_date")
    return TelegramWebhook(
        configured=True,
        bot=bot,
        expected_url=_expected(config),
        url=info.get("url") or None,
        pending_update_count=info.get("pending_update_count"),
        last_error_date=datetime.fromtimestamp(when, UTC) if isinstance(when, int) else None,
        last_error_message=info.get("last_error_message"),
    )


@admin_router.get("/telegram", response_model=TelegramWebhook, responses={403: {"model": ErrorBody}})
async def show_webhook(request: Request, user: AdminUser) -> TelegramWebhook:
    """The hub's bot and where Telegram sends its updates."""
    return await _webhook_view(request)


@admin_router.post(
    "/telegram/webhook",
    response_model=TelegramWebhook,
    responses={
        403: {"model": ErrorBody},
        409: {"model": ErrorBody},
        502: {"model": ErrorBody},
        503: {"model": ErrorBody},
    },
)
async def set_webhook(request: Request, user: AdminUser) -> TelegramWebhook:
    """Point the bot's webhook at this hub (EVO_HUB_PUBLIC_URL + /v1/telegram/webhook) with the secret token."""
    config = request.app.state.config
    if config.telegram_missing():
        raise HTTPException(503, NOT_SET_UP)
    url = _expected(config)
    if url is None:
        raise HTTPException(409, "the hub has no EVO_HUB_PUBLIC_URL, so Telegram has nowhere to send updates")
    try:
        await BotApi.from_config(config).call(
            "setWebhook", url=url, secret_token=config.telegram_webhook_secret, allowed_updates=ALLOWED_UPDATES
        )
    except BotApiError as exc:
        raise HTTPException(502, f"Telegram did not set the webhook: {exc}") from None
    async with request.app.state.engine.begin() as conn:
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=WEBHOOK_SET, target=url)
    log.info("telegram webhook set", extra={"login": user.login})
    return await _webhook_view(request)


class TelegramUnlinked(BaseModel):
    login: str
    unlinked: bool = Field(description="a chat was linked to the member, and is not any more")


@admin_router.delete(
    "/users/{login}/telegram",
    response_model=TelegramUnlinked,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def unlink_member(request: Request, login: Login, user: AdminUser) -> TelegramUnlinked:
    """Unlink a member's Telegram chat, whatever its session: the channel and the deliveries waiting for it go."""
    u = tables.users
    async with request.app.state.engine.begin() as conn:
        named = select(u.c.id, u.c.login).where(func.lower(u.c.login) == func.lower(login))
        found = (await conn.execute(named)).one_or_none()
        if found is None:
            raise HTTPException(404, f"the hub has no user {login}")
        unlinked = await _unlink(conn, found.id, found.login, "admin", actor=user)
    if unlinked:
        log.info("telegram unlinked", extra={"login": found.login, "by": "admin", "admin": user.login})
    return TelegramUnlinked(login=found.login, unlinked=unlinked)
