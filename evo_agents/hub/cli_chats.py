"""``evo-agents hub chat create|list|show|send|react``: the group chats the signed-in member is in
(``evo_agents.hub.chats``), over /v1/chats.

``create`` makes a chat with a title, ``--member LOGIN`` and ``--agent HANDLE`` repeated for the people and agents it
starts with (adding an agent needs the writer role on its project). ``list`` prints the member's chats, the latest
active first, with the messages of others they have not read. ``show`` prints a chat, its people and agents, and its
latest messages (``--limit``, or one thread with ``--thread``), each with its mentions, reactions and the thread it is
in. ``send`` sends a message, from the argument or from a file (``--file PATH``, ``-`` for stdin), in the thread of
``--reply-to``; a ``@name`` of a member mentions them. ``react`` puts an emoji on a message, ``--remove`` takes it off.
``--json`` prints what the hub answered. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.chats import MAX_MESSAGE_BYTES, emoji_problem
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.contract import json_option, returns_array, returns_object

EXIT_USAGE = 2
CHAT_KEYS = (
    "id",
    "title",
    "created_by",
    "created_at",
    "people",
    "agents",
    "label",
    "restricted",
    "last_seq",
    "last_read_seq",
    "budget_usd",
    "kind",
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
)
PAGE_KEYS = ("chat", "messages", "more")


def _path(chat_id: int | None = None) -> str:
    return "/v1/chats" if chat_id is None else f"/v1/chats/{chat_id}"


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _author(author: dict) -> str:
    if author["kind"] == "agent":
        return f"@{author['handle']}" + (" (deleted)" if author.get("deleted") else "")
    return author.get("login") or author["name"]


def _reactions(message: dict) -> str:
    parts = []
    for reaction in message["reactions"]:
        who = [*reaction["people"], *(f"@{handle}" for handle in reaction["agents"])]
        parts.append(f"{reaction['emoji']} {reaction['count']} ({', '.join(who)})")
    return "; ".join(parts)


def _print_message(message: dict) -> None:
    thread = f", in the thread of #{message['thread_root_id']}" if message["thread_root_id"] else ""
    replies = f", {message['reply_count']} replies" if message["reply_count"] else ""
    print(f"#{message['id']} {_author(message['author'])} at {when(message['created_at'])} UTC{thread}{replies}")
    for line in message["body"].splitlines() or [""]:
        print(f"  {line}")
    refused = [m["refusal"] for m in message["mentions"] if m.get("refusal")]
    if refused:
        print(f"  refused mentions: {'; '.join(refused)}")
    if message["reactions"]:
        print(f"  reactions: {_reactions(message)}")


def _print_chat(chat: dict) -> None:
    print(f"Chat {chat['id']}: {chat['title']}, created by {chat['created_by']} at {when(chat['created_at'])} UTC")
    print(f"  people: {', '.join(p['login'] for p in chat['people']) or '-'}")
    agents = [f"@{a['handle']} ({a['name']}, {a['project']})" for a in chat["agents"]]
    print(f"  agents: {', '.join(agents) or '-'}")


# Commands


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path())
    if args.json:
        print_json(found)
        return 0
    if not found:
        print("You are in no chat: create one with `evo-agents hub chat create TITLE`.")
        return 0
    rows = [
        (c["id"], c["title"], c["unread"], ", ".join(c["people"]), ", ".join(f"@{h}" for h in c["agents"]) or "-")
        for c in found
    ]
    table(("ID", "TITLE", "UNREAD", "PEOPLE", "AGENTS"), rows)
    return 0


@client_command
def cmd_create(args) -> int:
    hub, _ = signed_in()
    body = {
        "title": args.title,
        "people": list(args.member or []),
        "agents": [handle.removeprefix("@") for handle in args.agent or []],
    }
    chat = hub.call("POST", _path(), body)
    if args.json:
        print_json(chat)
    else:
        _print_chat(chat)
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    query = [f"limit={args.limit}"] + ([f"thread={args.thread}"] if args.thread else [])
    page = hub.call("GET", f"{_path(args.chat)}/messages?{'&'.join(query)}")
    if args.json:
        print_json(page)
        return 0
    _print_chat(page["chat"])
    if page["more"]:
        print("  (older messages left out: raise --limit)")
    for message in page["messages"]:
        _print_message(message)
    return 0


def _text(args) -> str:
    if args.file is None:
        if args.text is None:
            raise ValueError("give the message as an argument, or --file PATH (- for stdin)")
        return args.text
    if args.text is not None:
        raise ValueError("give the message as an argument or with --file, not both")
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text("utf-8")
    if len(text.encode()) > MAX_MESSAGE_BYTES:
        raise ValueError(f"the message is {len(text.encode())} bytes, over the {MAX_MESSAGE_BYTES} one takes")
    return text


@client_command
def cmd_send(args) -> int:
    try:
        body = {"body": _text(args)}
    except (ValueError, OSError) as exc:
        return _usage(str(exc))
    if args.reply_to:
        body["reply_to"] = args.reply_to
    hub, _ = signed_in()
    message = hub.call("POST", f"{_path(args.chat)}/messages", body)
    if args.json:
        print_json(message)
    else:
        _print_message(message)
    return 0


@client_command
def cmd_react(args) -> int:
    problem = None if args.remove else emoji_problem(args.emoji)
    if problem is not None:
        return _usage(problem)
    hub, _ = signed_in()
    path = f"{_path(args.chat)}/messages/{args.message}/reactions"
    if args.remove:
        message = hub.call("DELETE", f"{path}?emoji={quote(args.emoji, safe='')}")
    else:
        message = hub.call("POST", path, {"emoji": args.emoji})
    if args.json:
        print_json(message)
    else:
        print(f"#{message['id']}: {_reactions(message) or 'no reactions'}")
    return 0


# Registering


def register_chats(hsub) -> None:
    chat = hsub.add_parser("chat", help="the group chats you are in: people and agents of projects, with @mentions")
    csub = chat.add_subparsers(dest="chat_command", required=True)

    listed = csub.add_parser("list", help="your chats, the latest active first, with what you have not read")
    json_option(listed, returns_array(*SUMMARY_KEYS, schema="ChatSummary"))
    listed.set_defaults(func=cmd_list)

    create = csub.add_parser("create", help="create a chat with the people and agents it starts with")
    create.add_argument("title", metavar="TITLE", help="its title, one line of at most 120 characters")
    create.add_argument("--member", action="append", metavar="LOGIN", help="a person to add; repeat it")
    create.add_argument(
        "--agent", action="append", metavar="HANDLE", help="an agent to add (writer on its project); repeat it"
    )
    json_option(create, returns_object(*CHAT_KEYS, schema="Chat"))
    create.set_defaults(func=cmd_create)

    show = csub.add_parser("show", help="a chat, its people and agents, and its latest messages")
    show.add_argument("chat", metavar="CHAT", type=int, help="the chat's id (`evo-agents hub chat list`)")
    show.add_argument("--thread", type=int, metavar="MESSAGE", help="only the thread this message starts")
    show.add_argument("--limit", type=int, default=50, metavar="N", help="the latest N messages (default: 50)")
    json_option(show, returns_object(*PAGE_KEYS, schema="ChatPage"))
    show.set_defaults(func=cmd_show)

    send = csub.add_parser("send", help="send a message; @name mentions a member")
    send.add_argument("chat", metavar="CHAT", type=int, help="the chat's id")
    send.add_argument("text", metavar="TEXT", nargs="?", help="the message")
    send.add_argument("--file", metavar="PATH", help="read the message from a file, - for stdin")
    send.add_argument("--reply-to", type=int, metavar="MESSAGE", help="answer this message, in its thread")
    json_option(send, returns_object(*MESSAGE_KEYS, schema="GroupMessage"))
    send.set_defaults(func=cmd_send)

    react = csub.add_parser("react", help="put an emoji on a message, or take it off")
    react.add_argument("chat", metavar="CHAT", type=int, help="the chat's id")
    react.add_argument("message", metavar="MESSAGE", type=int, help="the message's id")
    react.add_argument("emoji", metavar="EMOJI", help="one emoji")
    react.add_argument("--remove", action="store_true", help="take your emoji off instead")
    json_option(react, returns_object(*MESSAGE_KEYS, schema="GroupMessage"))
    react.set_defaults(func=cmd_react)
