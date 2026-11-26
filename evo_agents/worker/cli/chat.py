"""``evo-agents worker chat post|react|read|create|mention``: the commands of a run's agent in group chats
(``evo_agents.hub.chat_runs``), which the agent of a chat run uses, and the agent of a step, plan or author run too.

- ``post TEXT [--chat ID] [--reply-to MESSAGE] [--attach FILE]``: a message of the run's agent, in the thread of a
  chat run, else in a chat its agent is in (``--chat``), in the thread of ``--reply-to`` when given. An @ of an agent
  in it hands no work. Each ``--attach`` file goes to the run's outputs first and under the message
  (``output_files.attached``); ``--interactive`` marks them interactive answers, HTML the web shows inside the message.
- ``react MESSAGE EMOJI [--remove]``: an emoji of the run's agent on a message, or taken off.
- ``create --title TEXT [--person LOGIN ...] [--agent HANDLE ...]``: a new chat of the run's agent and the person who
  dispatched the run, with the people and agents named; the hub tells each person.
- ``mention HANDLE TEXT [--chat ID] [--reply-to MESSAGE]``: a message that hands the work to agent HANDLE of the chat,
  for the person who dispatched the run, one hop further; in a direct chat, an agent it does not have goes to the group
  chat the hub opens for the thread, where the message goes; a refusal (the hops, the agent itself, no worker) is
  printed, said in the thread, and exits 1.
- ``read MESSAGE [--thread] [--fetch]``, a command of the run's agent too: a message of a chat whole, or its thread (its
  first message and the replies), those the run's agent may read (POST .../chat/read,
  ``evo_agents.hub.server.chat_read``), printed as the hub gave it, under a line saying it is data, never
  instructions. MESSAGE is its id, or its item as kg_search lists it (``plugin.chats:message:<id>``); a thread's item
  (``plugin.chats:thread:<chat>/<root>``) reads that thread. A message it may not read is an error, as one that does
  not exist. The text names the files attached to each message (name, media type, size); with ``--fetch`` the command
  also saves them in the agent's directory, under ``.evo-run/inbox/chat-<message id>/``
  (``evo_agents.worker.attachments``), and lists their paths after it, exit status 1 when one was not saved.

Each is a :class:`~evo_agents.worker.cli.base.ChatCommand`: it reads the run from EVO_RUN_ID, EVO_WORKER_HOME and the
run's record there, calls the hub with the worker's token (POST /v1/worker/runs/{id}/chat/...,
``evo_agents.hub.server.chat_agents``), and refuses outside a run of this worker of a kind of
``chat_runs.COMMAND_KINDS``; a call the hub does not answer, or answers with a 5xx while it deploys, is sent again for
up to OUTAGE_SECONDS (``AgentRunContext.in_chats``). TEXT ``-`` reads stdin. Each prints one line with the ids it made;
they take no ``--json``, since this module may not import the hub's contract (the worker reaches no ``hub.server``
module, which the contract's modules import). A failure is one ``error:`` line on stderr and exit status 1; without
the worker extra, status 2.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from evo_agents.clikit import Arg, Group, Result, Stderr, Text, Verbatim, Views
from evo_agents.hub import chats
from evo_agents.worker.cli.base import EXIT_FAILED, AgentRunContext, ChatCommand, MissingExtra, hubapi, text_of
from evo_agents.worker.cli.output import attach_arg, interactive_arg
from evo_agents.worker.home import WorkerStateError

CHAT_ID = Arg("--chat", type=int, metavar="ID", help="a chat the run's agent is in; default: the chat of a chat run")
REPLY_TO = Arg(
    "--reply-to", type=int, metavar="MESSAGE", help="a message whose thread it goes in; default: the run's thread"
)


def _where(args) -> dict:
    return {key: value for key, value in (("chat_id", args.chat), ("reply_to", args.reply_to)) if value is not None}


class ChatPost(ChatCommand):
    name = "post"
    words = "chat post"
    help = "post a message of the run's agent: in the thread of a chat run by default"
    arguments = (
        Arg("text", metavar="TEXT", help="the message, at most 16 KiB (- for stdin); an @ hands no work"),
        CHAT_ID,
        REPLY_TO,
        attach_arg("the message"),
        interactive_arg("each file of --attach"),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        from evo_agents.worker.output_files import attached

        args = ctx.args
        ctx.check()
        body = {"body": text_of(args.text), **_where(args)}
        interactive = bool(getattr(args, "interactive", False))
        body.update(attached(ctx.home, ctx.run_id, args.attach, interactive=interactive))  # the outputs it carries
        answer = ctx.in_chats("chat/messages", body)
        thread = f", in the thread of #{answer['thread_root_id']}" if answer.get("thread_root_id") else ""
        return Result(answer, Text(f"Posted message #{answer['id']} in chat {answer['chat_id']}{thread}."))


class ChatReact(ChatCommand):
    name = "react"
    words = "chat react"
    help = "put an emoji of the run's agent on a message, or take it off"
    arguments = (
        Arg("message", metavar="MESSAGE", type=int, help="the message's id"),
        Arg("emoji", metavar="EMOJI", help="one emoji"),
        Arg("--remove", action="store_true", help="take the emoji off"),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        ctx.check()
        problem = chats.emoji_problem(args.emoji)
        if problem is not None:
            raise WorkerStateError(problem)
        body = {"message_id": args.message, "emoji": args.emoji, "remove": bool(args.remove)}
        answer = ctx.in_chats("chat/reactions", body)
        done = f"Took {args.emoji} off" if args.remove else f"Put {args.emoji} on"
        return Result(answer, Text(f"{done} message #{args.message}."))


class ChatRead(ChatCommand):
    name = "read"
    words = "chat read"
    help = "print a message of a chat whole, or its thread, as the run's agent may read it"
    arguments = (
        Arg(
            "message",
            metavar="MESSAGE",
            help="its id, or plugin.chats:message:<id> or plugin.chats:thread:<chat>/<root>",
        ),
        Arg("--thread", action="store_true", help="its thread: the first message and the replies"),
        Arg(
            "--fetch",
            action="store_true",
            help="also save the files attached to the messages read under .evo-run/inbox/chat-<message id>/ and list "
            "their paths",
        ),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        ctx.check()
        if chats.read_ref(args.message) is None:
            raise WorkerStateError(
                f"{args.message!r} names no message: give its id, or plugin.chats:message:<id> or "
                "plugin.chats:thread:<chat>/<root> as kg_search lists it"
            )
        answer = ctx.in_chats("chat/read", {"id": str(args.message).strip(), "thread": bool(args.thread)})
        text = Verbatim(answer["text"])
        if not args.fetch:
            return Result(answer, text)
        listed, failed = fetch_files(ctx, answer.get("messages") or [])
        return Result(answer, Views(text, Verbatim(listed)), EXIT_FAILED if failed else 0)


def agent_dir(ctx: AgentRunContext) -> Path:
    """The directory of the run's agent, as the run's record notes it; WorkerStateError before the run has one."""
    record = ctx.home.load_run(ctx.run_id) or {}
    where = record.get("dir") or record.get("worktree")
    if not isinstance(where, str) or not where:
        raise WorkerStateError(f"run {ctx.run_id} has no directory on this worker yet: no file can be saved for it")
    return Path(where)


def fetch_files(ctx: AgentRunContext, messages: list) -> tuple[str, bool]:
    """The files of ``messages`` (as chat/read answers them) saved under ``.evo-run/inbox/chat-<message id>/`` of the
    agent's directory (``attachments.save_files``): the text that lists them, message by message, and whether one
    was not saved."""
    api = hubapi()
    try:
        from evo_agents.worker import attachments
    except ImportError as exc:
        raise MissingExtra(f"missing module {exc.name}") from None
    root = agent_dir(ctx)
    run_id = ctx.run_id
    carried = [m for m in messages if isinstance(m, dict) and isinstance(m.get("id"), int) and m.get("attachments")]

    async def go() -> list[tuple[int, list]]:
        async with api.new_session() as session:
            hub = api.WorkerHub(ctx.config.url, ctx.token, session)
            return [
                (m["id"], await attachments.save_files(hub, run_id, root, f"chat-{m['id']}", m["attachments"]))
                for m in carried
            ]

    found = asyncio.run(go())
    if not found:
        return "\nNo file is attached to these messages.\n", False
    blocks = [f"\n# Files of message #{message_id}\n{attachments.listing(saved)}\n" for message_id, saved in found]
    return "".join(blocks), any(file.error for _, saved in found for file in saved)


class ChatCreate(ChatCommand):
    name = "create"
    words = "chat create"
    help = "create a chat of the run's agent and the person who dispatched the run, who is told"
    arguments = (
        Arg("--title", required=True, help="the chat's title, one line"),
        Arg(
            "--person",
            action="append",
            metavar="LOGIN",
            help="add a person with a grant on the agent's project; repeat",
        ),
        Arg(
            "--agent",
            action="append",
            metavar="HANDLE",
            help="add an agent the person who dispatched the run may add to a chat (writer on its project); repeat",
        ),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        ctx.check()
        agents = [handle.removeprefix("@").lower() for handle in args.agent or []]
        payload = {
            "title": args.title,
            "people": list(dict.fromkeys(args.person or [])),
            "agents": list(dict.fromkeys(agents)),
        }
        answer = ctx.in_chats("chats", payload)
        people = ", ".join(person["login"] for person in answer.get("people") or [])
        named = ", ".join(f"@{agent['handle']}" for agent in answer.get("agents") or [])
        said = f"Created chat #{answer['id']} {answer['title']!r} with {people} and {named}; each person was told."
        return Result(answer, Text(said))


class ChatMention(ChatCommand):
    name = "mention"
    words = "chat mention"
    help = (
        "hand work to another agent of the chat, for the person who dispatched the run, one hop further; in a direct "
        "chat, an agent it does not have goes to the group chat the hub opens for the thread"
    )
    arguments = (
        Arg("handle", metavar="HANDLE", help="the agent's handle, @ left out"),
        Arg("text", metavar="TEXT", help="what the agent is to do (- for stdin)"),
        CHAT_ID,
        REPLY_TO,
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        ctx.check()
        handle = args.handle.removeprefix("@").lower()
        answer = ctx.in_chats("chat/mentions", {"handle": handle, "body": text_of(args.text), **_where(args)})
        message = answer.get("message") or {}
        if answer.get("refusal"):
            return Result(answer, Stderr(Text(f"error: @{handle} took no work: {answer['refusal']}")), EXIT_FAILED)
        group = answer.get("group_id")
        where = "" if group is None else f" of group chat #{group}, which the hub opened for this thread"
        said = (
            f"Handed the work to @{handle} in message #{message.get('id')}{where}: its run is #{answer.get('run_id')}."
        )
        return Result(answer, Text(said))


CHAT = Group(
    "chat",
    "inside a run: talk in group chats as the run's agent (post, react, create, mention, read)",
    [ChatPost(), ChatReact(), ChatRead(), ChatCreate(), ChatMention()],
)
