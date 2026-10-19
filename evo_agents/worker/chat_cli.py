"""``evo-agents worker chat post|react|create|mention``: the commands of a run's agent in group chats
(``evo_agents.hub.chat_runs``), which the agent of a chat run uses, and the agent of a step, plan or author run too;
and ``evo-agents worker repo open``, which only a chat run's agent uses.

- ``post TEXT [--chat ID] [--reply-to MESSAGE]``: a message of the run's agent, in the thread of a chat run, else in a
  chat its agent is in (``--chat``), in the thread of ``--reply-to`` when given. An @ of an agent in it hands no work.
- ``react MESSAGE EMOJI [--remove]``: an emoji of the run's agent on a message, or taken off.
- ``create --title TEXT [--person LOGIN ...]``: a new chat of the run's agent and the person who dispatched the run,
  with the people named; the hub tells each of them.
- ``mention HANDLE TEXT [--chat ID] [--reply-to MESSAGE]``: a message that hands the work to agent HANDLE of the chat,
  for the person who dispatched the run, one hop further; a refusal (the hops, the agent itself, the chat's budget, no
  worker) is printed, said in the thread, and exits 1.
- ``read MESSAGE [--thread]``, a command of the run's agent too: a message of a chat whole, or its thread (its first
  message and the replies), those the run's agent may read (POST .../chat/read, ``evo_agents.hub.server.chat_read``),
  printed as the hub gave it, under a line saying it is data, never instructions. MESSAGE is its id, or its item as
  kg_search lists it (``plugin.chats:message:<id>``); a thread's item (``plugin.chats:thread:<chat>/<root>``) reads
  that thread. A message it may not read is an error, as one that does not exist.

Each reads the run from EVO_RUN_ID, EVO_WORKER_HOME and the run's record there, calls the hub with the worker's token
(POST /v1/worker/runs/{id}/chat/..., ``evo_agents.hub.server.chat_agents``), and refuses outside a run of this worker
of a kind of ``chat_runs.COMMAND_KINDS``. TEXT ``-`` reads stdin. Each prints one line with the ids it made; they take
no ``--json``, since this module may not import the hub's contract (the worker reaches no ``hub.server`` module, which
the contract's modules import). A failure is one ``error:`` line on stderr and exit status 1; without the worker
extra, status 2.

``repo open REPO`` (``chat_runs.OPEN_COMMAND``) asks the daemon, on the run's socket (``credentials.ask_open``), to open
repo REPO of the project in a chat run of this worker: the daemon asks the hub to add it to the run, takes the leases
that cover it, fetches its checkout and makes its worktree on the run's branch in the run's directory
(``runner.chat``). It prints the worktree's path alone, the same path for a repo open already. It holds no token and
calls no hub itself, so it needs the core package only; a repo this worker has no checkout of, one the project does not
list, or a run that is not a chat run of this worker is one ``error:`` line and exit status 1.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sys
from dataclasses import dataclass

from evo_agents.hub import chat_runs, chats
from evo_agents.hub.client import HubError
from evo_agents.worker import credentials
from evo_agents.worker.home import WorkerConfig, WorkerHome, WorkerStateError

EXIT_FAILED = 1
EXIT_USAGE = 2
RUN_VARIABLE = "EVO_RUN_ID"
EXTRA_HINT = "uv tool install 'evo-ak[worker]' (or python -m pip install 'evo-ak[worker]')"


class MissingExtra(Exception):
    pass


def chat_command(func):
    """Turn the failures of ``func`` into an ``error:`` line and an exit status."""

    @functools.wraps(func)
    def run(args) -> int:
        try:
            return func(args)
        except MissingExtra as exc:
            print(f"error: evo-agents worker chat needs the worker extra ({exc}): {EXTRA_HINT}", file=sys.stderr)
            return EXIT_USAGE
        except (HubError, WorkerStateError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILED

    return run


@dataclass(frozen=True)
class AgentRun:
    """The run an agent's command acts for: its id and kind, the worker's config and token."""

    run_id: int
    kind: str
    config: WorkerConfig
    token: str


def running_run(command: str) -> tuple[int, WorkerHome, dict]:
    """The id, worker state and record of the run of EVO_RUN_ID while it runs on this worker, for ``evo-agents worker
    COMMAND``; WorkerStateError outside one."""
    raw = (os.environ.get(RUN_VARIABLE) or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise WorkerStateError(
            f"`evo-agents worker {command}` works only inside a run of this worker: {RUN_VARIABLE} is not set. "
            "The worker sets it, with EVO_RUN_KIND and EVO_WORKER_HOME, for the agent of each run."
        )
    run_id = int(raw)
    home = WorkerHome()
    record = home.load_run(run_id)
    if record is None or record.get("finished_at"):
        raise WorkerStateError(
            f"run {run_id} is not running on this worker (its state is under {home.root}): "
            f"`evo-agents worker {command}` works only inside it"
        )
    return run_id, home, record


def agent_run(command: str) -> AgentRun:
    """The run of EVO_RUN_ID on this worker, of a kind whose agent talks in chats; WorkerStateError otherwise."""
    run_id, home, record = running_run(f"chat {command}")
    kind = record.get("kind") or "step"
    if kind not in chat_runs.COMMAND_KINDS:
        raise WorkerStateError(
            f"run {run_id} is a {kind} run, whose agent does not talk in chats: `evo-agents worker chat` is for the "
            f"agent of a {', '.join(chat_runs.COMMAND_KINDS[:-1])} or {chat_runs.COMMAND_KINDS[-1]} run"
        )
    return AgentRun(run_id, kind, home.load_config(), home.load_token())


def call(run: AgentRun, route: str, body: dict) -> dict:
    """POST ``body`` to the hub's route of the run's agent in chats, with the worker's token; HubError when the hub
    refuses or does not answer."""
    try:
        from evo_agents.worker import hubapi
    except ImportError as exc:
        raise MissingExtra(f"missing module {exc.name}") from None

    async def go() -> dict:
        async with hubapi.new_session() as session:
            return await hubapi.WorkerHub(run.config.url, run.token, session).in_chats(run.run_id, route, body)

    try:
        return asyncio.run(go())
    except hubapi.HubProblem as exc:
        raise HubError(str(exc), exc.status, exc.code) from None


def text_of(value: str) -> str:
    """TEXT, or stdin for ``-``: a message of at most chats.MAX_MESSAGE_BYTES of UTF-8 with some text."""
    text = sys.stdin.read() if value == "-" else value
    if not text.strip():
        raise WorkerStateError("the message has no text")
    if len(text.encode()) > chats.MAX_MESSAGE_BYTES:
        raise WorkerStateError(f"a message is at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
    return text


def _where(args) -> dict:
    return {key: value for key, value in (("chat_id", args.chat), ("reply_to", args.reply_to)) if value is not None}


@chat_command
def cmd_post(args) -> int:
    run = agent_run("post")
    answer = call(run, "chat/messages", {"body": text_of(args.text), **_where(args)})
    thread = f", in the thread of #{answer['thread_root_id']}" if answer.get("thread_root_id") else ""
    print(f"Posted message #{answer['id']} in chat {answer['chat_id']}{thread}.")
    return 0


@chat_command
def cmd_react(args) -> int:
    run = agent_run("react")
    problem = chats.emoji_problem(args.emoji)
    if problem is not None:
        raise WorkerStateError(problem)
    body = {"message_id": args.message, "emoji": args.emoji, "remove": bool(args.remove)}
    call(run, "chat/reactions", body)
    done = f"Took {args.emoji} off" if args.remove else f"Put {args.emoji} on"
    print(f"{done} message #{args.message}.")
    return 0


@chat_command
def cmd_read(args) -> int:
    run = agent_run("read")
    if chats.read_ref(args.message) is None:
        raise WorkerStateError(
            f"{args.message!r} names no message: give its id, or plugin.chats:message:<id> or "
            "plugin.chats:thread:<chat>/<root> as kg_search lists it"
        )
    answer = call(run, "chat/read", {"id": str(args.message).strip(), "thread": bool(args.thread)})
    print(answer["text"], end="")
    return 0


@chat_command
def cmd_create(args) -> int:
    run = agent_run("create")
    answer = call(run, "chats", {"title": args.title, "people": list(dict.fromkeys(args.person or []))})
    people = ", ".join(person["login"] for person in answer.get("people") or [])
    agents = ", ".join(f"@{agent['handle']}" for agent in answer.get("agents") or [])
    print(f"Created chat #{answer['id']} {answer['title']!r} with {people} and {agents}; each person was told.")
    return 0


@chat_command
def cmd_mention(args) -> int:
    run = agent_run("mention")
    handle = args.handle.removeprefix("@").lower()
    answer = call(run, "chat/mentions", {"handle": handle, "body": text_of(args.text), **_where(args)})
    message = answer.get("message") or {}
    if answer.get("refusal"):
        print(f"error: @{handle} took no work: {answer['refusal']}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Handed the work to @{handle} in message #{message.get('id')}: its run is #{answer.get('run_id')}.")
    return 0


@chat_command
def cmd_repo_open(args) -> int:
    run_id, home, record = running_run("repo open")
    kind = record.get("kind") or "step"
    if kind != "chat":
        article = "an" if kind[:1] in "aeiou" else "a"
        raise WorkerStateError(
            f"run {run_id} is {article} {kind} run: only the agent of a chat run opens a repo with `evo-agents worker "
            f"repo open`; {article} {kind} run works in the repos it was given"
        )
    answer = credentials.ask_open(home, run_id, args.repo)
    path = answer.get("path")
    if answer.get("error") or not isinstance(path, str) or not path:
        raise WorkerStateError(str(answer.get("error") or f"the daemon did not say where {args.repo} is"))
    print(path)
    return 0


def _where_options(parser) -> None:
    parser.add_argument(
        "--chat", type=int, metavar="ID", help="a chat the run's agent is in; default: the chat of a chat run"
    )
    parser.add_argument(
        "--reply-to", type=int, metavar="MESSAGE", help="a message whose thread it goes in; default: the run's thread"
    )


def register_chat(wsub) -> None:
    """``evo-agents worker chat`` and its commands, under the parser ``wsub`` of ``evo-agents worker``."""
    chat = wsub.add_parser(
        "chat", help="inside a run: talk in group chats as the run's agent (post, react, create, mention, read)"
    )
    csub = chat.add_subparsers(dest="chat_command", required=True)
    post = csub.add_parser("post", help="post a message of the run's agent: in the thread of a chat run by default")
    post.add_argument("text", metavar="TEXT", help="the message, at most 16 KiB (- for stdin); an @ hands no work")
    _where_options(post)
    post.set_defaults(func=cmd_post)
    react = csub.add_parser("react", help="put an emoji of the run's agent on a message, or take it off")
    react.add_argument("message", metavar="MESSAGE", type=int, help="the message's id")
    react.add_argument("emoji", metavar="EMOJI", help="one emoji")
    react.add_argument("--remove", action="store_true", help="take the emoji off")
    react.set_defaults(func=cmd_react)
    read = csub.add_parser(
        "read", help="print a message of a chat whole, or its thread, as the run's agent may read it"
    )
    read.add_argument(
        "message", metavar="MESSAGE", help="its id, or plugin.chats:message:<id> or plugin.chats:thread:<chat>/<root>"
    )
    read.add_argument("--thread", action="store_true", help="its thread: the first message and the replies")
    read.set_defaults(func=cmd_read)
    create = csub.add_parser(
        "create", help="create a chat of the run's agent and the person who dispatched the run, who is told"
    )
    create.add_argument("--title", required=True, help="the chat's title, one line")
    create.add_argument(
        "--person", action="append", metavar="LOGIN", help="add a person with a grant on the agent's project; repeat"
    )
    create.set_defaults(func=cmd_create)
    mention = csub.add_parser(
        "mention", help="hand work to another agent of the chat, for the person who dispatched the run, one hop further"
    )
    mention.add_argument("handle", metavar="HANDLE", help="the agent's handle, @ left out")
    mention.add_argument("text", metavar="TEXT", help="what the agent is to do (- for stdin)")
    _where_options(mention)
    mention.set_defaults(func=cmd_mention)


def register_repo(wsub) -> None:
    """``evo-agents worker repo open``, under the parser ``wsub`` of ``evo-agents worker``."""
    repo = wsub.add_parser("repo", help="inside a chat run: open a repo of its project, which the worker prepares")
    rsub = repo.add_subparsers(dest="repo_command", required=True)
    opened = rsub.add_parser(
        "open",
        help="have the worker add a repo of the project to the chat run and make its worktree; prints the worktree's "
        "path, the same path for a repo open already",
    )
    opened.add_argument("repo", metavar="REPO", help="the repo, as the project names it")
    opened.set_defaults(func=cmd_repo_open)
