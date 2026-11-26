"""``evo-agents hub hook session-start|stop|post-tool-use|user-prompt|local-tool|session-end|local-ask|pre-tool-use``:
the Claude Code hook entry points of the evo-hub plugin, as classes of ``evo_agents.clikit``.

The work of each hook lives in its own module, which holds no command: ``evo_agents.hub.hooks`` (SessionStart, Stop,
SessionEnd), ``evo_agents.hub.spool`` (PostToolUse), ``evo_agents.hub.local_sync`` (the local run's hooks) and
``evo_agents.hub.guard_hook`` (PreToolUse of Bash). A :class:`Hook` runs that work within its budget, writes its own
answer to Claude Code as it goes (JSON on stdout, one line on stderr) and always exits 0: a hook never fails the
session. The modules other than ``hooks`` are imported when their hook runs, so the others load none of them.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from abc import abstractmethod
from typing import ClassVar, TextIO

from evo_agents.clikit import Command, Context, Group, Result
from evo_agents.hub import hooks


def say(stream: TextIO, text: str) -> None:
    with contextlib.suppress(OSError, ValueError):  # a closed stdout or stderr must not fail the hook either
        print(text, file=stream, flush=True)


class Hook(Command[Context]):
    """A hook of the evo-hub plugin: :meth:`answer` writes what Claude Code reads and catches every failure itself."""

    def context(self, args: argparse.Namespace) -> Context:
        return Context(args)

    def run(self, ctx: Context) -> Result:
        self.answer(ctx.out)
        return Result(None)  # written already; the exit status is 0

    @abstractmethod
    def answer(self, out: TextIO) -> None: ...


class SessionStart(Hook):
    name = "session-start"
    help = (
        "pull the memories of the session's directory, export the plans of its harness, count the skills to "
        "sync, and print one line for the session"
    )

    def answer(self, out: TextIO) -> None:
        line = hooks.Line()
        seconds = hooks.BUDGETS["session-start"]
        try:
            with hooks.budget(seconds):
                hooks.session_start(hooks.read_payload(), line)
        except hooks.OutOfTime:
            line.add(
                f"stopped after {seconds:g}s with what was done; the rest waits for the next session", trouble=True
            )
        except BaseException as exc:  # a hook never fails the session
            line.add(hooks.failure("the SessionStart hook failed", exc), trouble=True)
        text = line.text()
        output: dict = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        if line.trouble:
            output["systemMessage"] = text
        say(out, json.dumps(output, ensure_ascii=False))


class Stop(Hook):
    name = "stop"
    help = (
        "push the memory files of the session's directory that changed since the last sync and the session's "
        "digest, and queue its transcript for the hub"
    )

    def answer(self, out: TextIO) -> None:
        outcome = hooks.Outcome()
        seconds = hooks.BUDGETS["stop"]
        try:
            with hooks.budget(seconds):
                hooks.stop(hooks.read_payload(), outcome)
        except hooks.OutOfTime:
            outcome.quiet = f"evo-hub: memory push stopped after {seconds:g}s; what is left goes at a later Stop"
        except BaseException as exc:  # a hook never fails the session
            outcome.quiet = "evo-hub: " + hooks.failure("the Stop hook failed", exc)
        if outcome.loud:
            say(out, json.dumps({"systemMessage": hooks.one_line(outcome.loud)}, ensure_ascii=False))
        if outcome.quiet:
            say(sys.stderr, hooks.one_line(outcome.quiet))


class PostToolUse(Hook):
    name = "post-tool-use"
    help = (
        "write a tool result longer than EVO_SPOOL_THRESHOLD characters to a file under ~/.evo/tool-output and "
        "give the model its head, its tail and the file's path instead"
    )

    def answer(self, out: TextIO) -> None:
        """Print the replacement of a long result, then clean up old spool files; a failure before the replacement is
        printed leaves the result as it was and says why on stderr, by the exception's type alone."""
        from evo_agents.hub import spool

        env, now, answer = os.environ, time.time(), None
        try:
            with hooks.budget(spool.BUDGET):
                answer = spool.post_tool_use(hooks.read_payload(limit=spool.MAX_PAYLOAD), env, now)
                if answer is not None:
                    say(out, json.dumps(answer, ensure_ascii=False))
                    spool.prune(spool.spool_root(), spool.keep_days(env) * 86400, now)
        except hooks.OutOfTime:
            if answer is None:
                say(sys.stderr, f"evo-hub: tool output left as it was, out of time after {spool.BUDGET:g}s")
        except BaseException as exc:  # a hook never fails the session
            if answer is None:
                say(sys.stderr, f"evo-hub: tool output left as it was: unexpected {type(exc).__name__}")


class LocalHook(Hook):
    """A hook of the session's local run on the hub (``evo_agents.hub.local_sync``), named ``hook`` there."""

    hook: ClassVar[str]

    def answer(self, out: TextIO) -> None:
        from evo_agents.hub import local_outputs, local_sync

        try:
            with hooks.budget(local_sync.BUDGET[self.hook]):
                payload = local_outputs.read_payload() if self.hook == "tool" else hooks.read_payload()
                notes = local_sync.hook_run(self.hook, payload)
                if notes:
                    said = {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": local_sync.answers_context(notes),
                        }
                    }
                    out.write(json.dumps(said, ensure_ascii=False) + "\n")
                    out.flush()
        except hooks.OutOfTime:
            pass
        except BaseException as exc:  # a hook never fails the session
            with contextlib.suppress(OSError, ValueError):  # a closed stderr must not fail the hook either
                print(f"evo-hub: local session not synced: unexpected {type(exc).__name__}", file=sys.stderr)


class UserPrompt(LocalHook):
    name = "user-prompt"
    help = (
        "register the session as a local run of its hub project at its first turn and report it running "
        "(EVO_HUB_LOCAL_SYNC=0 turns it off)"
    )
    hook = "user-prompt"


class LocalTool(LocalHook):
    name = "local-tool"
    help = (
        "put the agent's words and the tool that ran, redacted, in the log of the session's local run, and queue "
        "the files it sent and the images of its result as the run's outputs"
    )
    hook = "tool"


class SessionEnd(Hook):
    name = "session-end"
    help = (
        "push the session's digest and those that waited, report its local run ended, and queue its transcript "
        "for the hub"
    )

    def answer(self, out: TextIO) -> None:
        outcome = hooks.Outcome()
        seconds = hooks.BUDGETS["session-end"]
        try:
            with hooks.budget(seconds):
                hooks.session_end(hooks.read_payload(), outcome)
        except hooks.OutOfTime:
            outcome.quiet = f"evo-hub: SessionEnd stopped after {seconds:g}s; a digest left waits for a later Stop"
        except BaseException as exc:  # a hook never fails the session
            outcome.quiet = "evo-hub: " + hooks.failure("the SessionEnd hook failed", exc)
        if outcome.quiet:
            say(sys.stderr, hooks.one_line(outcome.quiet))


class LocalAsk(LocalHook):
    name = "local-ask"
    help = (
        "put the questions of an AskUserQuestion dialog about to open to the hub as decisions of the session's "
        "local run, so the member can answer them there too; it does not wait"
    )
    hook = "ask"


class PreToolUse(Hook):
    name = "pre-tool-use"
    help = (
        "deny a Bash command that would commit, push or write with gh a private name of the hub's projects to a "
        "repo they publish (the core plugin guard's policy, kept at SessionStart)"
    )

    def answer(self, out: TextIO) -> None:
        """PreToolUse of Bash: deny a command that would put a private name in a guarded repo."""
        from evo_agents.hub import guard_hook

        try:
            reason = guard_hook.reason_to_deny(json.loads(sys.stdin.read() or "{}"))
        except Exception as exc:  # the hook never blocks a session for its own failure
            print(f"evo-hub: the guard of private names did not run: {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        if reason:
            decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
            print(json.dumps({"hookSpecificOutput": decision}, ensure_ascii=False), file=out)


PRE_TOOL_USE = PreToolUse()
HOOK = Group(
    "hook",
    "Claude Code hook entry points of the evo-hub plugin; they always exit 0",
    [
        SessionStart(),
        Stop(),
        PostToolUse(),
        UserPrompt(),
        LocalTool(),
        SessionEnd(),
        LocalAsk(),
        PRE_TOOL_USE,
    ],
    dest="hook_name",
)
