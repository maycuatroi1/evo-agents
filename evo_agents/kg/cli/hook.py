"""``evo-agents kg hook``: the Claude Code hook entry points, and :func:`run_hook`, which runs one without the rest of
the command tree.

``session-start`` prints a short note on the bound project; ``post-tool`` joins the labels of an evo-kg result into
the session label; ``post-search`` says what the graph links to the lines a Grep, rg or grep matched, as the hub
answers it (``evo_agents.hub.kg_grep_hook``; ``evo_agents.cli.main`` runs it before building any parser).
"""

from __future__ import annotations

import argparse
import json
import sys

from evo_agents.clikit import Group, Result, Text
from evo_agents.kg.cli.base import KgCommand, KgContext
from evo_agents.kg.project import ProjectError


def hook_payload() -> dict:
    """The JSON object Claude Code writes to a hook's stdin, or {} when there is none."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        payload = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


class SessionStart(KgCommand):
    """SessionStart hook for Claude Code: a short note on the bound project, and the session label when the session
    already read from the graph (a resumed session). Only names and counts the pipeline derived; never source text,
    which would reach the model with system-reminder authority. When to call the graph is said once, in the
    instructions of the server that serves the kg_* tools, not here again."""

    name = "session-start"
    help = "print the SessionStart note"

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.client import HubError
        from evo_agents.hub.kg_remote import PROBE_TIMEOUT, hub_status
        from evo_agents.kg.sessions import current_session_id, describe, read_session
        from evo_agents.kg.status import render_brief

        try:
            _, status = hub_status(None, timeout=PROBE_TIMEOUT)
        except (ProjectError, HubError):
            return Result(None)  # not inside a project, not signed in, or the hub does not answer in time: say nothing
        note = render_brief(status)
        session = read_session(current_session_id(hook_payload()))
        if session is not None:
            note += "\n" + describe(session)
        return Result(
            None, Text(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note}}))
        )


class PostTool(KgCommand):
    """PostToolUse hook for the evo-kg tools: join the labels of the result into the session label. Prints nothing to
    stdout and never blocks: any error becomes a note on stderr and exit code 0."""

    name = "post-tool"
    help = "add an evo-kg result to the session label"

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.sessions import post_tool

        try:
            post_tool(json.loads(sys.stdin.read()))
        except Exception as exc:  # a hook must never break the session
            print(f"evo-kg post-tool hook skipped: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return Result(None)


class PostSearch(KgCommand):
    """PostToolUse hook for Grep and rg or grep in Bash: what the graph links to the matched lines, as
    additionalContext, as the hub answers it (``evo_agents.kg.post_search``)."""

    name = "post-search"
    help = "PostToolUse: what the graph links to a Grep, rg or grep match"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        from evo_agents.kg.post_search import add_arguments

        add_arguments(parser)

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.kg_grep_hook import run

        return Result(None, status=run(ctx.args))


HOOK = Group("hook", "Claude Code hook entry points", [SessionStart(), PostTool(), PostSearch()], dest="hook_name")


def run_hook(argv: list[str]) -> int:
    """``evo-agents kg hook ...`` without the rest of the command tree. Claude Code runs a hook on every matching
    tool call, and building every command group's parser costs about 0.2 s, more than the hook itself."""
    parser = argparse.ArgumentParser(prog="evo-agents kg hook", description="Claude Code hook entry points")
    below = parser.add_subparsers(dest="hook_name", required=True)
    for child in HOOK.children:
        child.attach(below)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)
