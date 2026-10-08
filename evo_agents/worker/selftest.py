"""``evo-agents worker selftest --runtime NAME``: the runtime's adapter, for real, on a tiny prompt in a scratch git
repository, the way the daemon drives it in a run (``start``, the events, ``wait``), without a hub.

The agent is asked to create ``selftest.txt`` holding ``ok`` and to answer DONE, so the run shows that the agent works
in its directory with the permissions of a run and that its events come through. Each event is printed as it comes,
then a summary: the versions checked, how many events of each kind, the session id and how the turn ended. The exit
status is 0 when the turn completed, at least one event came and the file holds ``ok``; 1 otherwise.

``--background`` (Claude Code only) checks instead that a session waits for a command the agent runs in the
background: the agent is asked to start ``sleep 20 && date -u`` with ``run_in_background``, end its turn without
waiting, and write ``selftest.txt`` only once it is told the command finished. It passes when, on top of the above,
the agent's turn ended while the command ran, the command's end came, and the file was written after it; it then
prints ``Run done``.

A real call spends the owner's quota, so the prompt is short and the reasoning effort of Claude Code and Codex is
``low`` unless ``--effort`` says otherwise; ``--model`` names the model (opencode needs one when its configured
default does not exist). The scratch repository is removed unless ``--keep`` is given.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from evo_agents.hub import runs
from evo_agents.worker.adapter import AgentEvent, Outcome, RunContext, load_adapters
from evo_agents.worker.runtimes.common import package_version

PROMPT = (
    "This is a self-test of an evo-agents worker. Create a file named selftest.txt in the current directory, "
    "containing exactly the word ok, then reply with the single word DONE."
)
RESULT_FILE = "selftest.txt"
BACKGROUND_COMMAND = "sleep 20 && date -u"
BACKGROUND_PROMPT = (
    f"This is a self-test of an evo-agents worker. With the Bash tool, run the command `{BACKGROUND_COMMAND}` with "
    "run_in_background set to true. Do not wait for it and do not check on it: once it has started, end your turn by "
    "replying STARTED, without creating any file. When you are told that it has finished, create a file named "
    f"{RESULT_FILE} in the current directory, containing exactly the word ok, then reply with the single word DONE."
)
BACKGROUND_RUNTIMES = ("claude-code",)  # the runtimes whose agent runs commands in the background
WRITE_SLACK = 1.0  # seconds a file's time may be earlier than the end of the command it waited for
DEFAULT_EFFORT = {"claude-code": "low", "codex": "low"}
PACKAGES = {"claude-code": "claude-agent-sdk", "codex": "openai-codex", "opencode": "aiohttp"}
DEFAULT_TIMEOUT = 300
END_GRACE = 60.0  # seconds for an interrupted agent to end
LINE_CHARS = 120
_GIT = {
    "GIT_AUTHOR_NAME": "evo-agents selftest",
    "GIT_AUTHOR_EMAIL": "selftest@localhost",
    "GIT_COMMITTER_NAME": "evo-agents selftest",
    "GIT_COMMITTER_EMAIL": "selftest@localhost",
}


def scratch_repo() -> Path:
    """A new git repository with one commit, in the temporary directory."""
    path = Path(tempfile.mkdtemp(prefix="evo-worker-selftest-")).resolve()
    env = {**os.environ, **_GIT}
    (path / "README.md").write_text("A scratch repository for `evo-agents worker selftest`.\n", encoding="utf-8")
    for command in (["init", "--quiet"], ["add", "README.md"], ["commit", "--quiet", "-m", "selftest"]):
        subprocess.run(["git", "-C", str(path), *command], check=True, capture_output=True, env=env)
    return path


class BackgroundWatch:
    """What the events of ``--background`` tell of its command: the agent's call that starts it in the background, a
    turn of the agent that ends while it runs, and its end, as Claude Code reports them."""

    def __init__(self) -> None:
        self.call_id: str | None = None
        self.started_at: float | None = None
        self.turn_ended = False  # a turn ended while the command ran
        self.ended_at: float | None = None  # wall-clock time its end came
        self.status: str | None = None

    def see(self, event: AgentEvent) -> str | None:
        """Follow ``event``; a line to print when it says something of the command."""
        body = event.body
        if event.kind == "tool_call" and self.call_id is None:
            given = body.get("rawInput")
            if (
                isinstance(given, dict)
                and given.get("run_in_background") in (True, "true")
                and BACKGROUND_COMMAND in str(given.get("command") or "")
            ):
                self.call_id = str(body.get("toolCallId") or "")
                self.started_at = time.time()
                return f"The agent started `{BACKGROUND_COMMAND}` in the background."
        elif self.call_id is None or self.ended_at is not None:
            return None
        elif event.kind == "usage_update" and not self.turn_ended:
            self.turn_ended = True
            return "The agent's turn ended while the command runs: the session waits for it."
        elif event.kind == "output":
            found = body.get("raw")
            if (
                isinstance(found, dict)
                and found.get("subtype") == "task_notification"
                and found.get("tool_use_id") == self.call_id
            ):
                self.ended_at = time.time()
                self.status = str(found.get("status") or "")
                took = self.ended_at - (self.started_at or self.ended_at)
                return f"The background command ended ({self.status}) {took:.0f}s after it started."
        return None

    def problems(self, repo: Path) -> list[str]:
        if self.call_id is None:
            return [f"the agent did not start `{BACKGROUND_COMMAND}` in the background"]
        found = []
        if not self.turn_ended:
            found.append("no turn of the agent ended while the command ran, so the wait for it was not checked")
        if self.ended_at is None:
            found.append("the end of the background command never came")
            return found
        if self.status != "completed":
            found.append(f"the background command ended {self.status or 'without a status'}")
        try:
            written_at = (repo / RESULT_FILE).stat().st_mtime
        except OSError:
            return found
        if written_at < self.ended_at - WRITE_SLACK:
            found.append(f"the agent wrote {RESULT_FILE} before the background command ended")
        return found


def describe(event: AgentEvent) -> str:
    """One event in a line: its kind and what it says."""
    body = event.body
    if event.kind in ("agent_message_chunk", "agent_thought_chunk"):
        text = (body.get("content") or {}).get("text") or ""
    elif event.kind == "tool_call":
        text = f"{body.get('title')} ({body.get('status')})"
    elif event.kind == "tool_call_update":
        text = f"{body.get('toolCallId')} {body.get('status')}"
    elif event.kind == "usage_update":
        text = json.dumps({"usage": body.get("usage"), "cost": body.get("cost")}, ensure_ascii=False)
    elif event.kind == "plan":
        text = f"{len(body.get('entries') or [])} entries"
    else:
        found = body.get("raw") if isinstance(body.get("raw"), dict) else body
        text = " ".join(str(found.get(key)) for key in ("type", "subtype", "method") if found.get(key)) or "event"
    text = " ".join(str(text).split())
    if len(text) > LINE_CHARS:
        text = text[: LINE_CHARS - 3] + "..."
    return f"  {event.kind}: {text}"


async def selftest(
    cls,
    runtime: str,
    *,
    model: str | None,
    effort: str | None,
    timeout: float,
    keep: bool,
    background: bool = False,
) -> int:
    repo = scratch_repo()
    run = {"id": 0, "project": "selftest", "plan_id": "selftest", "step_key": "selftest", "title": "worker selftest"}
    run.update({"runtime": runtime, "mode": "headless", "model": model, "effort": effort})
    env = {**os.environ, "EVO_RUN_ID": "0", "EVO_RUN_PROJECT": "selftest", "EVO_RUN_PLAN": "selftest"}
    prompt = BACKGROUND_PROMPT if background else PROMPT
    adapter = cls(RunContext(run=run, worktree=repo, prompt=prompt, env=env))
    counts: collections.Counter = collections.Counter()
    watch = BackgroundWatch() if background else None
    print(
        f"Self-test of {runtime} in {repo}"
        + (f", model {model}" if model else "")
        + (f", effort {effort}" if effort else "")
        + (f", with `{BACKGROUND_COMMAND}` in the background" if background else "")
    )
    try:
        try:
            await adapter.start()
        except Exception as exc:
            print(f"error: {runtime} did not start: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"Started, session {adapter.session_id}.")

        async def pump() -> None:
            async for event in adapter.events():
                counts[event.kind] += 1
                print(describe(event), flush=True)
                said = watch.see(event) if watch is not None else None
                if said:
                    print(said, flush=True)

        reader = asyncio.create_task(pump())
        done, _ = await asyncio.wait({reader}, timeout=timeout)
        if reader not in done:
            print(f"The agent ran past {timeout:g}s; interrupting it.")
            await adapter.interrupt()
            done, _ = await asyncio.wait({reader}, timeout=END_GRACE)
            if reader not in done:
                reader.cancel()
        try:
            outcome = await asyncio.wait_for(adapter.wait(), END_GRACE)
        except TimeoutError:
            outcome = Outcome(False, f"{runtime} did not end within {END_GRACE:g}s")
        return report(cls, runtime, adapter, outcome, counts, repo, watch)
    finally:
        if keep:
            print(f"Kept {repo}.")
        else:
            shutil.rmtree(repo, ignore_errors=True)


def report(cls, runtime, adapter, outcome: Outcome, counts, repo: Path, watch: BackgroundWatch | None = None) -> int:
    detection = cls.detect()
    versions = [f"{runtime} {detection.version or 'unknown'}"]
    package = PACKAGES.get(runtime)
    if package and package_version(package):
        versions.append(f"{package} {package_version(package)}")
    server = getattr(adapter, "server_version", None)
    if server:
        versions.append(f"app-server {server}")
    total = sum(counts.values())
    kinds = ", ".join(f"{kind} {counts[kind]}" for kind in runs.WORKER_EVENT_KINDS if counts[kind])
    print(f"Checked with {', '.join(versions)}.")
    print(f"{total} events" + (f" ({kinds})" if kinds else "") + f", session {adapter.session_id}.")
    try:
        written = (repo / RESULT_FILE).read_text(encoding="utf-8").strip().lower() == "ok"
    except OSError:
        written = False
    if outcome.usage:
        print(f"Usage: {json.dumps(outcome.usage, ensure_ascii=False)}")
    if outcome.summary:
        print(f"Last message: {outcome.summary[:LINE_CHARS]}")
    problems = []
    if not outcome.completed:
        problems.append(f"the turn did not complete: {outcome.error or 'no reason given'}")
    if total == 0:
        problems.append("no event came")
    if not written:
        problems.append(f"the agent did not write {RESULT_FILE} with ok")
    if watch is not None:
        problems.extend(watch.problems(repo))
    if problems:
        print(f"Self-test of {runtime} failed: {'; '.join(problems)}.", file=sys.stderr)
        return 1
    if watch is not None:
        print(
            f"Run done: the session waited for `{BACKGROUND_COMMAND}`, which ran in the background, and the agent "
            f"wrote {RESULT_FILE} after it ended."
        )
    print(f"Self-test of {runtime} passed: the turn completed and {RESULT_FILE} holds ok.")
    return 0


def cmd_selftest(args) -> int:
    cls = load_adapters().get(args.runtime)
    if cls is None:
        print(f"error: this worker has no adapter for {args.runtime}", file=sys.stderr)
        return 1
    if args.background and args.runtime not in BACKGROUND_RUNTIMES:
        print(
            f"error: --background checks the background commands of {', '.join(BACKGROUND_RUNTIMES)}; "
            f"{args.runtime} runs none",
            file=sys.stderr,
        )
        return 2
    detection = cls.detect()
    if not detection.available:
        print(f"error: {args.runtime} is unavailable here: {detection.reason}", file=sys.stderr)
        return 1
    effort = args.effort if args.effort is not None else DEFAULT_EFFORT.get(args.runtime)
    return asyncio.run(
        selftest(
            cls,
            args.runtime,
            model=args.model,
            effort=effort or None,
            timeout=args.timeout,
            keep=args.keep,
            background=args.background,
        )
    )


def add_parser(wsub) -> None:
    parser = wsub.add_parser(
        "selftest",
        help="run a runtime's adapter on a tiny prompt in a scratch repository and print its events (spends quota)",
    )
    parser.add_argument("--runtime", required=True, choices=runs.RUNTIMES, help="the runtime to check")
    parser.add_argument("--model", help="the model; opencode takes provider/model")
    parser.add_argument(
        "--effort", help="the reasoning effort (low for claude-code and codex unless given; an empty value: default)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"seconds before the agent is interrupted ({DEFAULT_TIMEOUT})",
    )
    parser.add_argument("--keep", action="store_true", help="keep the scratch repository")
    parser.add_argument(
        "--background",
        action="store_true",
        help=(
            "check that the session waits for a command run in the background: the agent starts "
            f"`{BACKGROUND_COMMAND}` that way, ends its turn, and writes the file once it ended (claude-code only)"
        ),
    )
    parser.set_defaults(func=cmd_selftest)
