"""``evo-agents hub chat create|list|show|send|react|label|unbridge``: the group chats the signed-in member is in
(``evo_agents.hub.chats``), over /v1/chats.

``create`` makes a chat with a title, ``--member LOGIN`` and ``--agent HANDLE`` repeated for the people and agents it
starts with (adding an agent needs the writer role on its project). ``list`` prints the member's chats, the latest
active first, with the messages of others they have not read. ``show`` prints a chat, its people and agents, and its
latest messages (``--limit``, or one thread with ``--thread``), each with its mentions, reactions and the thread it is
in. ``send`` sends a message, from the argument or from a file (``--file PATH``, ``-`` for stdin), in the thread of
``--reply-to``; a ``@name`` of a member mentions them. ``--attach FILE`` (repeated) uploads files the message
carries (``evo_agents.hub.cli.attach``); with one, the text may be left out. ``react`` puts an emoji on a message,
``--remove`` takes it off. ``label CHAT LEVEL`` sets the chat's level, at or below the default level of every project of
its agents (the admin role on each), and ``label CHAT --default`` takes it off. ``show`` lists the groups the chat is
bridged to (``docs/chat-bridges.md``), each message the kind of channel it came in by and the bridges it went out to;
``unbridge CHAT BRIDGE`` takes a bridge off, as a chat admin or a hub admin. ``--json`` prints what the hub answered.
Standard library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Chats` is the
gateway to /v1/chats, and :data:`CHAT` their group.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, returns_array, returns_object
from evo_agents.hub.chats import MAX_MESSAGE_BYTES, emoji_problem
from evo_agents.hub.cli import attach as attach_cli
from evo_agents.hub.cli.base import HubCommand, HubContext, HubRoutes, when

CHAT_KEYS = (
    "id",
    "title",
    "created_by",
    "created_at",
    "people",
    "agents",
    "label",
    "label_level",
    "label_levels",
    "label_admin",
    "restricted",
    "last_seq",
    "last_read_seq",
    "kind",
    "image_url",
    "bridges",
)
SUMMARY_KEYS = (
    "id",
    "title",
    "created_by",
    "created_at",
    "last_message_at",
    "last_seq",
    "unread",
    "people",
    "agents",
    "kind",
    "image_url",
    "direct_agent",
    "faces",
    "last_message",
    "work",
)
MESSAGE_KEYS = (
    "id",
    "chat_id",
    "seq",
    "thread_root_id",
    "reply_count",
    "author",
    "run_id",
    "body",
    "mentions",
    "reactions",
    "label",
    "created_at",
    "progress",
    "outputs",
    "attachments",
    "via",
    "bridged_to",
    "from_answer",
    "forwarded_by",
    "work",
)
PAGE_KEYS = ("chat", "messages", "more")


class Chats(HubRoutes):
    """GET and POST /v1/chats and the routes of one chat below it (Gateway)."""

    path = "/v1/chats"

    @staticmethod
    def of_chat(chat: int) -> str:
        """The path of one chat, which the uploads of a message's files go under."""
        return f"{Chats.path}/{chat}"

    def list(self) -> Any:
        return self.call("GET")

    def create(self, body: dict) -> Any:
        return self.call("POST", "", body)

    def page(self, chat: int, limit: int, thread: int | None) -> Any:
        query = {"limit": limit, **({"thread": thread} if thread else {})}
        return self.call("GET", f"/{chat}/messages?{urlencode(query)}")

    def send(self, chat: int, body: dict) -> Any:
        return self.call("POST", f"/{chat}/messages", body)

    def react(self, chat: int, message: int, emoji: str) -> Any:
        return self.call("POST", f"/{chat}/messages/{message}/reactions", {"emoji": emoji})

    def unreact(self, chat: int, message: int, emoji: str) -> Any:
        return self.call("DELETE", f"/{chat}/messages/{message}/reactions?emoji={quote(emoji, safe='')}")

    def label(self, chat: int, level: str) -> Any:
        return self.call("PUT", f"/{chat}/label", {"level": level})

    def unlabel(self, chat: int) -> Any:
        return self.call("DELETE", f"/{chat}/label")

    def unbridge(self, chat: int, bridge: int) -> Any:
        return self.call("DELETE", f"/{chat}/bridges/{bridge}")


# What a person reads


def author_text(author: dict) -> str:
    if author["kind"] == "agent":
        return f"@{author['handle']}" + (" (deleted)" if author.get("deleted") else "")
    if author["kind"] == "guest":
        return f"{author['name']} (guest)"
    return author.get("login") or author["name"]


def reactions_text(message: dict) -> str:
    parts = []
    for reaction in message["reactions"]:
        who = [*reaction["people"], *(f"@{handle}" for handle in reaction["agents"])]
        parts.append(f"{reaction['emoji']} {reaction['count']} ({', '.join(who)})")
    return "; ".join(parts)


def message_lines(message: dict) -> list[str]:
    thread = f", in the thread of #{message['thread_root_id']}" if message["thread_root_id"] else ""
    replies = f", {message['reply_count']} replies" if message["reply_count"] else ""
    via = f", from {message['via']}" if message.get("via") else ""
    head = (
        f"#{message['id']} {author_text(message['author'])} at {when(message['created_at'])} UTC{via}{thread}{replies}"
    )
    lines = [head, *(f"  {line}" for line in message["body"].splitlines() or [""])]
    refused = [m["refusal"] for m in message["mentions"] if m.get("refusal")]
    if refused:
        lines.append(f"  refused mentions: {'; '.join(refused)}")
    if message.get("attachments"):
        lines.append(f"  files: {attach_cli.describe(message['attachments'])}")
    if message["reactions"]:
        lines.append(f"  reactions: {reactions_text(message)}")
    return lines


def chat_lines(chat: dict) -> list[str]:
    lines = [
        f"Chat {chat['id']}: {chat['title']}, created by {chat['created_by']} at {when(chat['created_at'])} UTC",
        f"  people: {', '.join(p['login'] for p in chat['people']) or '-'}",
    ]
    agents = [f"@{a['handle']} ({a['name']}, {a['project']})" for a in chat["agents"]]
    lines.append(f"  agents: {', '.join(agents) or '-'}")
    levels = sorted({item["level"] for item in chat.get("label") or [] if isinstance(item.get("level"), str)})
    shown = ", ".join(levels) or "-"
    lines.append(f"  label: {shown}" + (f" (set to {chat['label_level']})" if chat.get("label_level") else ""))
    bridges = [f"#{b['id']} {b['kind']} {b['external_id']}" for b in chat.get("bridges") or []]
    if bridges:
        lines.append(f"  bridged to: {', '.join(bridges)}")
    return lines


CHAT_ID = Arg("chat", metavar="CHAT", type=int, help="the chat's id")


# Commands


class ChatList(HubCommand):
    name = "list"
    help = "your chats, the latest active first, with what you have not read"
    output = returns_array(*SUMMARY_KEYS, schema="ChatSummary")
    columns = ("ID", "TITLE", "UNREAD", "PEOPLE", "AGENTS")

    def run(self, ctx: HubContext) -> Result:
        found = Chats(ctx.hub).list()
        rows = [
            (c["id"], c["title"], c["unread"], ", ".join(c["people"]), ", ".join(f"@{h}" for h in c["agents"]) or "-")
            for c in found
        ]
        empty = "You are in no chat: create one with `evo-agents hub chat create TITLE`."
        return Result(found, Table(self.columns, rows, empty))


class ChatCreate(HubCommand):
    name = "create"
    help = "create a chat with the people and agents it starts with"
    arguments = (
        Arg("title", metavar="TITLE", help="its title, one line of at most 120 characters"),
        Arg("--member", action="append", metavar="LOGIN", help="a person to add; repeat it"),
        Arg("--agent", action="append", metavar="HANDLE", help="an agent to add (writer on its project); repeat it"),
    )
    output = returns_object(*CHAT_KEYS, schema="Chat")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {
            "title": args.title,
            "people": list(args.member or []),
            "agents": [handle.removeprefix("@") for handle in args.agent or []],
        }
        chat = Chats(ctx.hub).create(body)
        return Result(chat, Text(*chat_lines(chat)))


class ChatShow(HubCommand):
    name = "show"
    help = "a chat, its people and agents, and its latest messages"
    arguments = (
        Arg("chat", metavar="CHAT", type=int, help="the chat's id (`evo-agents hub chat list`)"),
        Arg("--thread", type=int, metavar="MESSAGE", help="only the thread this message starts"),
        Arg("--limit", type=int, default=50, metavar="N", help="the latest N messages (default: 50)"),
    )
    output = returns_object(*PAGE_KEYS, schema="ChatPage")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        page = Chats(ctx.hub).page(args.chat, args.limit, args.thread)
        if args.json:
            return Result(page)
        lines = chat_lines(page["chat"])
        if page["more"]:
            lines.append("  (older messages left out: raise --limit)")
        for message in page["messages"]:
            lines += message_lines(message)
        return Result(page, Text(*lines))


def message_text(args: argparse.Namespace) -> str:
    """The message to send: TEXT, or the file ``--file`` names (stdin for ``-``); ValueError when it is missing, given
    twice or too long."""
    if args.file is None:
        if args.text is None and not args.attach:
            raise ValueError("give the message as an argument, or --file PATH (- for stdin), or attach a file")
        return args.text or ""
    if args.text is not None:
        raise ValueError("give the message as an argument or with --file, not both")
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text("utf-8")
    if len(text.encode()) > MAX_MESSAGE_BYTES:
        raise ValueError(f"the message is {len(text.encode())} bytes, over the {MAX_MESSAGE_BYTES} one takes")
    return text


class ChatSend(HubCommand):
    name = "send"
    help = "send a message; @name mentions a member"
    arguments = (
        CHAT_ID,
        Arg("text", metavar="TEXT", nargs="?", help="the message; may be left out with --attach"),
        Arg("--attach", action="append", metavar="FILE", help=attach_cli.ATTACH_HELP),
        Arg("--file", metavar="PATH", help="read the message from a file, - for stdin"),
        Arg("--reply-to", type=int, metavar="MESSAGE", help="answer this message, in its thread"),
    )
    output = returns_object(*MESSAGE_KEYS, schema="GroupMessage")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        try:
            body: dict[str, Any] = {"body": message_text(args)}
            files = attach_cli.checked(args.attach or [])
        except (ValueError, OSError) as exc:
            raise UsageError(str(exc)) from None
        if args.reply_to:
            body["reply_to"] = args.reply_to
        chats = Chats(ctx.hub)
        if files:
            body["attachment_ids"] = attach_cli.upload(chats.hub, Chats.of_chat(args.chat), files)
        message = chats.send(args.chat, body)
        return Result(message, Text(*message_lines(message)))


class ChatReact(HubCommand):
    name = "react"
    help = "put an emoji on a message, or take it off"
    arguments = (
        CHAT_ID,
        Arg("message", metavar="MESSAGE", type=int, help="the message's id"),
        Arg("emoji", metavar="EMOJI", help="one emoji"),
        Arg("--remove", action="store_true", help="take your emoji off instead"),
    )
    output = returns_object(*MESSAGE_KEYS, schema="GroupMessage")

    def check(self, args: argparse.Namespace) -> None:
        problem = None if args.remove else emoji_problem(args.emoji)
        if problem is not None:
            raise UsageError(problem)

    def run(self, ctx: HubContext) -> Result:
        args, chats = ctx.args, Chats(ctx.hub)
        if args.remove:
            message = chats.unreact(args.chat, args.message, args.emoji)
        else:
            message = chats.react(args.chat, args.message, args.emoji)
        return Result(message, Text(f"#{message['id']}: {reactions_text(message) or 'no reactions'}"))


class ChatLabel(HubCommand):
    name = "label"
    help = "set the chat's level, at or below its projects' default (admin of each), or take it off"
    arguments = (
        CHAT_ID,
        Arg("level", metavar="LEVEL", nargs="?", help="a level of every project's ladder"),
        Arg("--default", action="store_true", help="take the level set off: the projects' default again"),
    )
    output = returns_object(*CHAT_KEYS, schema="Chat")

    def check(self, args: argparse.Namespace) -> None:
        if (args.level is None) == (not args.default):
            raise UsageError("give the chat's LEVEL, or --default to take the level set off")

    def run(self, ctx: HubContext) -> Result:
        args, chats = ctx.args, Chats(ctx.hub)
        chat = chats.unlabel(args.chat) if args.default else chats.label(args.chat, args.level)
        return Result(chat, Text(*chat_lines(chat)))


class ChatUnbridge(HubCommand):
    name = "unbridge"
    help = "take a bridge of the chat to a group off (an admin of its projects, or a hub admin)"
    arguments = (
        CHAT_ID,
        Arg("bridge", metavar="BRIDGE", type=int, help="the bridge's id (`evo-agents hub chat show`)"),
    )
    output = returns_object(*CHAT_KEYS, schema="Chat")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        chat = Chats(ctx.hub).unbridge(args.chat, args.bridge)
        return Result(chat, Text(f"Bridge {args.bridge} of chat {args.chat} is off.", *chat_lines(chat)))


CHAT = Group(
    "chat",
    "the group chats you are in: people and agents of projects, with @mentions",
    [ChatList(), ChatCreate(), ChatShow(), ChatSend(), ChatReact(), ChatLabel(), ChatUnbridge()],
)
