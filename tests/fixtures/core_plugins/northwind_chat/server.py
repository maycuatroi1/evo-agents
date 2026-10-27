"""The channel, the routes and the event of northwind-chat, through the API of core plugins alone.

- ``NorthwindChannel`` sends a member's notifications to their chat: a decision with a button per option, a proposal
  with accept, reject and defer, a notice; a project kept from the channel's clearance gets what waits and the link.
- A chat user asks for a link with ``/link``; the member opens it on the hub (GET, ``WebSession``) and confirms it
  (POST, with the hub's CSRF header and Origin), which links the chat to their web session. Or the member makes a
  one-time code on the hub (POST .../code) and sends ``/start <code>`` from the chat. DELETE unlinks it, and so does
  ``/stop`` in the chat; a hub admin unlinks a member's chat (``HubAdmin``).
- Northwind Chat calls the webhook ``/v1/hooks/northwind-chat/webhook/<secret>``: a button answers a decision or a
  proposal, a reply to a decision's message answers it in words, ``@handle text`` talks to an agent in the member's
  direct chat with it, ``/agents`` lists the agents to talk to.
- An agent's message in that direct chat comes back as the event ``chat.message``; the plugin reads it as the member
  and sends it to the chat, once.
- A group of Northwind Chat bridges to a group chat of the hub (API 1.5): a chat admin makes a code on the hub and
  sends ``/bridge <code>`` in the group; ``/unbridge`` there takes it off, and so does a hub admin. A linked member's
  message in the group goes to the hub's chat as theirs (``hub.bridge_send``), a reply in the thread of the message it
  answers, a file with it; the hub's chat's new messages come back as the event ``chat.bridge`` and go to the group,
  as far as the project's sink of kind channel ``northwind`` lets them (``hub.bridge_message``), a run's progress and
  a reaction as a line.
- Any chat user, linked or not, asks the agent type helpdesk of a project ``/ask <project> <question>``
  (``hub.ask_agent_type``); the answer comes back as the event ``agent.answer`` and goes to the chat that asked, whose
  ids its ref holds.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from evo_agents.hub.server import core_plugin_api as hub
from northwind_chat import KIND

TRANSPORT: httpx.AsyncBaseTransport | None = None  # the tests put the fake Northwind Chat here
LINK_PATH = "/v1/northwind-chat/link"
CODE_PATH = "/v1/northwind-chat/code"
ADMIN_PATH = "/v1/northwind-chat/admin/users/{user_id}"
BRIDGE_ADMIN_PATH = "/v1/northwind-chat/admin/bridges/{bridge_id}"
MAX_UPDATE_BYTES = 64 * 1024
KEEP_SENT = 50  # ids of agents' messages the channel remembers it sent
NOT_LINKED = "This chat is not linked to the hub: send /link and confirm it on the hub."
HELP = (
    "Tap a button to answer, reply to a decision with words, /link, /stop, /agents, @agent and your message, or "
    "/ask <project> <question>."
)
HELPDESK = "helpdesk"
EVERYONE = "*"  # the user_id of a message to the whole group
NOT_BRIDGED = "This group is not bridged to a chat of the hub."
ANSWERS = ("accept", "reject", "defer")

router = APIRouter(include_in_schema=False)  # the hub's OpenAPI document stays the same with or without the plugin


def plugin() -> hub.Plugin:
    return hub.plugin(__name__)


def cleared(values) -> hub.Clearance:
    found = values.get("clearance") or {}
    return hub.Clearance(found.get("level", ""), found.get("location"))


def signature(values, group_id: str, user_id: str) -> str:
    """What a link of chat user ``user_id`` in ``group_id`` carries, so only the chat that asked can be linked."""
    key = str(values.get("webhook_secret") or "").encode()
    return hmac.new(key, f"{group_id}:{user_id}".encode(), hashlib.sha256).hexdigest()


class Service:
    """Northwind Chat's API, with the bot's token."""

    def __init__(self, values):
        self.url = str(values.get("api_url", "")).rstrip("/")
        self.token = values.get("token")

    async def send(
        self, group_id: str, user_id: str, text: str, buttons: list | None = None, reply_to: str | None = None
    ) -> str:
        if not self.token:
            raise hub.Undeliverable("Northwind Chat is not set up: a hub admin sets the plugin's token")
        body = {"group_id": group_id, "user_id": user_id, "text": text, "buttons": buttons or []}
        if reply_to is not None:
            body["reply_to"] = reply_to
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=10) as client:
            answer = await client.post(
                f"{self.url}/messages", json=body, headers={"Authorization": f"Bearer {self.token}"}
            )
        if answer.status_code == 429:
            raise hub.RetryAfter(float(answer.json().get("retry_after", 5)), "Northwind Chat asked to wait")
        if answer.status_code == 403:
            raise hub.ChannelGone("Northwind Chat refused the chat: link it again")
        answer.raise_for_status()
        return str(answer.json()["id"])


# The channel


def message(notification: hub.Outgoing, url: str | None) -> tuple[str, list]:
    """The text and the buttons of ``notification`` as ``hub.prepare`` left it."""
    extra = notification.extra or {}
    where = url or notification.link or "the hub"
    if extra.get("restricted", True):
        return f"Something of {notification.project or 'the hub'} waits for you: {where}", []
    decision, proposal = extra.get("decision"), extra.get("proposal")
    if notification.kind == "decision":
        if not decision or decision["state"] != "open":
            return f"Decision #{notification.decision_id} is no longer open: {where}", []
        buttons = [
            {"text": item["label"], "data": f"d:{notification.decision_id}:{item['key']}"}
            for item in decision["options"] or []
        ]
        return f"Decision #{notification.decision_id}: {decision['question']}", buttons
    if notification.kind == "proposal":
        if not proposal or proposal["state"] != "open":
            return f"Proposal #{notification.proposal_id} is no longer open: {where}", []
        buttons = [{"text": action, "data": f"p:{notification.proposal_id}:{action}"} for action in ANSWERS]
        return f"Proposal #{notification.proposal_id}: {proposal['title']}", buttons
    return f"{notification.title}: {where}", []


class NorthwindChannel(hub.Channel):
    """Sends a member's notifications to their chat while the web session that linked it lives."""

    needs_session = True

    async def prepare(self, conn, notification: hub.Outgoing) -> hub.Outgoing:
        settings = await hub.plugin_config(conn, plugin(), self.config)
        self.values = settings.values
        return await hub.prepare(conn, notification, cleared(settings.values))

    async def send(self, notification: hub.Outgoing, config: dict) -> str | None:
        text, buttons = message(notification, hub.web_url(self.config, notification.link))
        return await Service(self.values).send(config["group_id"], config["user_id"], text, buttons)


# Linking, on the hub


class LinkIn(BaseModel):
    group_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    sig: str = Field(min_length=64, max_length=64)


async def _checked(request: Request, conn, body: LinkIn):
    settings = await hub.plugin_config(conn, plugin(), hub.hub_config(request))
    if not hmac.compare_digest(body.sig, signature(settings.values, body.group_id, body.user_id)):
        raise HTTPException(404, "no such link: ask for a new one with /link in the chat")
    return settings


@router.get(LINK_PATH)
async def show_link(request: Request, session: hub.WebSession, group_id: str, user_id: str, sig: str) -> dict:
    """What confirming the link would link, for the member to read before they confirm it."""
    body = LinkIn(group_id=group_id, user_id=user_id, sig=sig)
    async with hub.begin(request) as conn:
        await _checked(request, conn, body)
    return {"group_id": group_id, "user_id": user_id, "login": session.login}


@router.post(LINK_PATH, status_code=201)
async def link(request: Request, body: LinkIn, session: hub.WebSession) -> dict:
    """Link the chat to the caller's web session."""
    async with hub.begin(request) as conn:
        settings = await _checked(request, conn, body)
        config = {"group_id": body.group_id, "user_id": body.user_id}
        member = await hub.link_channel(conn, plugin(), session, KIND, config, unique_on=("group_id", "user_id"))
    await Service(settings.values).send(body.group_id, body.user_id, f"Linked to the hub as {member.login}.")
    return {"linked": True, "login": member.login}


@router.delete(LINK_PATH)
async def unlink(request: Request, session: hub.WebSession) -> dict:
    """Unlink the caller's chat."""
    async with hub.begin(request) as conn:
        gone = await hub.unlink_channel(conn, plugin(), KIND, session.user_id, by="web")
    return {"unlinked": gone}


@router.post(CODE_PATH, status_code=201)
async def code(request: Request, session: hub.WebSession) -> dict:
    """A one-time code the member sends from the chat as ``/start <code>``, within ten minutes."""
    async with hub.begin(request) as conn:
        made = await hub.link_code(conn, plugin(), session, KIND)
    return {"code": made.code, "expires_at": made.expires_at.isoformat()}


@router.delete(ADMIN_PATH)
async def unlink_member(request: Request, user_id: int, admin: hub.HubAdmin) -> dict:
    """A hub admin unlinks a member's chat, whatever its session."""
    async with hub.begin(request) as conn:
        gone = await hub.unlink_channel(conn, plugin(), KIND, user_id, by="admin", actor=admin)
    return {"unlinked": gone}


@router.delete(BRIDGE_ADMIN_PATH)
async def unbridge_group(request: Request, bridge_id: int, admin: hub.HubAdmin) -> dict:
    """A hub admin takes a group's bridge off, in any chat."""
    async with hub.begin(request) as conn:
        gone = await hub.bridge_unlink(conn, plugin(), bridge_id, by="admin", actor=admin)
    return {"unbridged": gone}


# The webhook


@router.post("/v1/hooks/northwind-chat/webhook/{secret}")
async def webhook(request: Request, secret: str) -> dict:
    """Northwind Chat's updates, with the plugin's webhook secret in the path: 404 for any other, before the body is
    read."""
    config = hub.hub_config(request)
    async with hub.begin(request) as conn:
        settings = await hub.plugin_config(conn, plugin(), config)
    expected = str(settings.values.get("webhook_secret") or "")
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(404, "Not Found")
    raw = await request.body()
    if len(raw) > MAX_UPDATE_BYTES:
        raise HTTPException(413, "an update of Northwind Chat is never this large")
    try:
        update = json.loads(raw)
    except ValueError:
        return {"outcome": "ignored"}
    if not isinstance(update, dict) or not update.get("group_id") or not update.get("user_id"):
        return {"outcome": "ignored"}
    chat = (str(update["group_id"]), str(update["user_id"]))
    outcome, reply = await Updates(request, settings.values, chat).handle(update)
    if reply:
        await Service(settings.values).send(*chat, reply)
    return {"outcome": outcome}


class Updates:
    """What the webhook does with one update of one chat."""

    def __init__(self, request: Request, values, chat: tuple[str, str]):
        self.request, self.values, self.chat = request, values, chat
        self.match = {"group_id": chat[0], "user_id": chat[1]}

    async def handle(self, update: dict) -> tuple[str, str | None]:
        text = str(update.get("text") or "").strip()
        if update.get("data"):
            return await self.acting(lambda conn, member: self.button(conn, member, str(update["data"])))
        if text == "/link":
            query = urlencode(
                {"group_id": self.chat[0], "user_id": self.chat[1], "sig": signature(self.values, *self.chat)}
            )
            url = hub.web_url(hub.hub_config(self.request), f"{LINK_PATH}?{query}")
            return "link", f"Open {url} on the hub and confirm it."
        if text.startswith("/start "):
            return await self.start(text.removeprefix("/start ").strip())
        if text == "/stop":
            async with hub.begin(self.request) as conn:
                member = await hub.find_member(conn, plugin(), KIND, self.match, live=False)
                if member is not None:
                    await hub.unlink_channel(conn, plugin(), KIND, member.user_id, by="bot")
            return "unlinked", "This chat is unlinked from the hub."
        if text.startswith("/bridge "):
            return await self.bridge(text.removeprefix("/bridge ").strip())
        if text == "/unbridge":
            return await self.unbridge()
        if text and not text.startswith("/") or update.get("file"):
            async with hub.begin(self.request) as conn:
                bridge = await hub.bridge_find(conn, plugin(), KIND, self.chat[0])
            if bridge is not None:
                return await self.bridged(bridge, update, text)
        if text == "/agents":
            return await self.acting(self.agents)
        if update.get("reply_to") and text:
            return await self.acting(lambda conn, member: self.in_words(conn, member, str(update["reply_to"]), text))
        if text.startswith("/ask "):
            project, _, question = text.removeprefix("/ask ").strip().partition(" ")
            return await self.ask(project, question.strip(), str(update.get("message_id") or len(text)))
        if text.startswith("@") and " " in text:
            handle, _, said = text[1:].partition(" ")
            return await self.acting(lambda conn, member: self.talk(conn, member, handle, said.strip()))
        return "help", HELP

    async def ask(self, project: str, question: str, message_id: str) -> tuple[str, str | None]:
        """A chat user's question to the help desk of ``project``, for whoever they are: no member is needed."""
        ref = ":".join(("northwind", *self.chat, message_id))
        try:
            async with hub.begin(self.request) as conn:
                asked = await hub.ask_agent_type(
                    conn,
                    plugin(),
                    project,
                    HELPDESK,
                    question,
                    asker=f"northwind user {self.chat[1]}",
                    ref=ref,
                    cleared=cleared(self.values),
                )
        except hub.Refused as exc:
            return "refused", f"The hub did not take that: {exc.detail}"
        return "asked", f"Asked the help desk of {asked.project} (question {asked.question_id})."

    async def bridge(self, code: str) -> tuple[str, str | None]:
        """Bridge this group to the hub's chat a chat admin made ``code`` for."""
        try:
            async with hub.begin(self.request) as conn:
                found = await hub.bridge_link(conn, plugin(), KIND, code, self.chat[0], config={"by": self.chat[1]})
        except hub.Refused as exc:
            return "refused", f"The hub did not take that: {exc.detail}"
        if found is None:
            return "bridge_failed", "That code did not work: make a new one on the hub."
        return "bridged", f"This group is bridged to chat {found.chat_id} of the hub."

    async def unbridge(self) -> tuple[str, str | None]:
        async with hub.begin(self.request) as conn:
            found = await hub.bridge_find(conn, plugin(), KIND, self.chat[0])
            if found is None:
                return "not_bridged", NOT_BRIDGED
            await hub.bridge_unlink(conn, plugin(), found.id, by="bot")
        return "unbridged", "This group is no longer bridged to the hub."

    async def bridged(self, bridge: hub.Bridge, update: dict, text: str) -> tuple[str, str | None]:
        """A message of the bridged group, from the member its sender linked (their own chat with the bot): it goes
        to the hub's chat as theirs; a sender who linked nothing is told how, and nothing goes in."""
        message_id = str(update.get("message_id") or "")
        if not message_id:
            return "ignored", None
        match = {"user_id": self.chat[1]}
        async with hub.begin(self.request) as conn:
            member = await hub.find_member(conn, plugin(), KIND, match)
        if member is None:
            return "not_linked", NOT_LINKED
        try:
            files = []
            attached = update.get("file")
            if isinstance(attached, dict):
                data = base64.b64decode(str(attached.get("data") or ""), validate=True)
                name = str(attached.get("name") or "file")
                files.append(await hub.bridge_attach(self.request, member, bridge.id, name, data))
            async with hub.begin(self.request) as conn:
                reply_to = update.get("reply_to")
                await hub.bridge_send(
                    conn,
                    member,
                    bridge.id,
                    text,
                    external_id=message_id,
                    reply_to=None if reply_to is None else str(reply_to),
                    attachment_ids=files,
                )
        except (binascii.Error, ValueError):
            return "ignored", None
        except hub.Refused as exc:
            return "refused", f"The hub did not take that: {exc.detail}"
        return "bridged_in", None

    async def start(self, code: str) -> tuple[str, str | None]:
        async with hub.begin(self.request) as conn:
            session = await hub.redeem_link_code(conn, plugin(), KIND, code)
            if session is None:
                return "link_failed", "That code did not work: make a new one on the hub."
            member = await hub.link_channel(
                conn, plugin(), session, KIND, self.match, unique_on=("group_id", "user_id")
            )
        return "linked", f"Linked to the hub as {member.login}."

    async def acting(self, act) -> tuple[str, str | None]:
        """``act(conn, member)`` for the member the chat is linked to, in one transaction; the hub's refusal is told
        to the chat, and nothing of it is kept."""
        try:
            async with hub.begin(self.request) as conn:
                member = await hub.find_member(conn, plugin(), KIND, self.match)
                if member is None:
                    return "not_linked", NOT_LINKED
                return await act(conn, member)
        except hub.Refused as exc:
            return "refused", f"The hub did not take that: {exc.detail}"

    async def button(self, conn, member: hub.Member, data: str) -> tuple[str, str | None]:
        kind, _, rest = data.partition(":")
        number, _, value = rest.partition(":")
        if kind not in ("d", "p") or not number.isdigit() or not value:
            return "ignored", "That button is not the hub's."
        if kind == "d":
            answer = await hub.answer_decision(conn, member, int(number), cleared(self.values), option=value)
            return "answered", f"Answered by {member.login}: {answer.option_label or value}"
        if value not in ANSWERS:
            return "ignored", "That button is not the hub's."
        done = await hub.answer_proposal(conn, member, int(number), cleared(self.values), value)
        return "answered", f"Proposal #{done.proposal_id} {done.state} by {member.login}"

    async def in_words(self, conn, member: hub.Member, replied: str, text: str) -> tuple[str, str | None]:
        found = await hub.delivered_notification(conn, member, replied)
        if found is None or found.decision_id is None:
            return "replied", "Only a decision takes an answer in words."
        answer = await hub.answer_decision(conn, member, found.decision_id, cleared(self.values), text=text)
        return "replied", f"Your answer to decision #{answer.decision_id} went to run #{answer.inbox_run}."

    async def talk(self, conn, member: hub.Member, handle: str, said: str) -> tuple[str, str | None]:
        chat = await hub.open_direct_chat(conn, member, handle)
        await hub.send_message(conn, member, chat.chat_id, said)
        return "sent", None

    async def agents(self, conn, member: hub.Member) -> tuple[str, str | None]:
        found = await hub.agents(conn, member)
        return "agents", "\n".join(f"@{agent.handle} ({agent.project})" for agent in found) or "No agent to talk to."


# The event


async def on_chat_message(params: dict, context: hub.Context) -> dict:
    """An agent's message in a direct chat of a member whose chat is linked: sent to the chat once, as the member
    reads it, or as a link when the chat's projects are kept from the channel."""
    payload = params["payload"]
    if payload.get("by") != "agent":
        return {}
    async with hub.transaction(context) as conn:
        member = await hub.direct_chat_member(conn, plugin(), KIND, payload["chat_id"])
        if member is None:
            return {}
        sent = list((member.config.get("state") or {}).get("sent", []))
        if payload["message_id"] in sent:
            return {}
        settings = await hub.plugin_config(conn, plugin(), context.config)
        try:
            read = await hub.read_message(
                conn, member, payload["chat_id"], payload["message_id"], cleared(settings.values)
            )
            text = f"@{read.author}: {read.body}"
        except hub.Refused:
            text = f"An agent answered in your direct chat on the hub: chat {payload['chat_id']}"
        await Service(settings.values).send(member.config["group_id"], member.config["user_id"], text)
        await hub.set_channel_state(conn, member, {"sent": [*sent, payload["message_id"]][-KEEP_SENT:]})
    return {}


async def on_agent_answer(params: dict, context: hub.Context) -> dict:
    """The end of a question to the help desk: its answer, read again from the hub, sent to the chat that asked."""
    payload = params["payload"]
    prefix, group_id, user_id, _ = (str(payload.get("ref")).split(":") + ["", "", "", ""])[:4]
    if prefix != "northwind" or not group_id or not user_id:
        return {}
    async with hub.transaction(context) as conn:
        found = await hub.read_agent_answer(conn, plugin(), payload["question_id"])
        settings = await hub.plugin_config(conn, plugin(), context.config)
    if found.state == "done":
        text = f"{found.agent_type} of {found.project}: {found.text}"
    else:
        text = f"The help desk of {found.project} gave no answer ({found.state})."
    await Service(settings.values).send(group_id, user_id, text)
    return {}


async def _bridge_line(conn, bridge: hub.Bridge, payload: dict) -> tuple[str | None, str | None]:
    """The line event ``payload`` sends the group of ``bridge``, and the group's message it replies to; None when
    nothing goes."""
    what = payload.get("what")
    if what == "message":
        read = await hub.bridge_message(conn, plugin(), bridge.id, payload["message_id"])
        if read is None or bridge.state.get("last_sent", 0) >= read.id:
            return None, None
        await hub.bridge_set_state(conn, plugin(), bridge.id, {**bridge.state, "last_sent": read.id})
        return f"{read.author or 'hub'}: {read.body}", read.reply_to
    if what == "progress":
        return f"run {payload['run_id']}: {payload['state']}", None
    if what == "reaction":
        sign = "+" if payload.get("added") else "-"
        return f"{sign}{payload['emoji']} on message {payload['message_id']}", None
    return None, None


async def on_chat_bridge(params: dict, context: hub.Context) -> dict:
    """What a bridged chat of the hub carries out to its group: a new message (once, as far as the hub lets it out),
    a run's progress and a reaction to a message the bridge carried, each a line."""
    payload = params["payload"]
    async with hub.transaction(context) as conn:
        bridge = await hub.bridge_read(conn, plugin(), payload["bridge_id"])
        if bridge is None:
            return {}
        settings = await hub.plugin_config(conn, plugin(), context.config)
        text, reply_to = await _bridge_line(conn, bridge, payload)
    if text is None:
        return {}
    sent = await Service(settings.values).send(bridge.external_id, EVERYONE, text, reply_to=reply_to)
    if payload["what"] == "message":
        async with hub.transaction(context) as conn:
            await hub.bridge_sent(conn, plugin(), bridge.id, payload["message_id"], sent)
    return {}
