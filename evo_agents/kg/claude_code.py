"""``evo-agents kg claude-entities``: the model command of the entities step (``evo_agents.kg.entities``) that asks
Claude Code, run headless with the subscription login of this machine, never an API key.

A mapping names it as its ``command`` (``["evo-agents", "kg", "claude-entities"]``) with the model, such as
``claude-haiku-5-5``, and a policy sink of kind ``llm-backend``: Claude Code sends the text to Anthropic, so the step
sends an item only when that sink clears the item's label (``entities.check`` refuses the mapping without a sink).

The command reads one request on stdin, ``{"text", "node_kinds", "relations", "model"}`` and the mapping's ``style``
when it is not ``closed`` (``entities.prompt``), or a ``task`` of ``entity_merge`` (merge, summary) whose ``text`` is the
whole prompt, answered with ``{"decisions"}`` or ``{"summary"}`` and the usage; it runs ``claude -p`` with that
model, no tool, no MCP server and no session kept, and writes one answer on stdout: ``{"nodes", "edges", "usage"}``,
where ``usage`` holds the tokens in (cache reads and writes included) and out and the cost Claude Code reports
(``total_cost_usd``; without it the step prices the tokens with the mapping's ``price``). ANTHROPIC_API_KEY and
ANTHROPIC_AUTH_TOKEN are taken out of Claude Code's environment, so the call runs on the login: the member's
``claude login`` on a machine, the lease of the pool's account (CLAUDE_CODE_OAUTH_TOKEN) in a run of the project's
default agent. Claude Code keeps that variable out of the shell of its tools, so in a run (EVO_RUN_ID) whose
environment lacks it, the call takes it from the run's socket (``run_login``). No value of the environment is ever
printed.

When Claude Code says the login is missing or refused, or that a usage or rate limit was reached, the command exits
STOP_EXIT with ``{"stop": "login" | "rate_limit", "error"}``: the step stops at that item and leaves the rest to a later
build, which goes on from the memo. Any other failure exits 1.

A machine without ``claude`` (the hub's build container) runs no call: the step defers the items its memo lacks
(``entities.run`` with an outbox), and a run of the project's default agent answers them with ``answer`` through
``evo-agents hub kg entities`` (``evo_agents.hub.kg_entities_cli``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

COMMAND = ("evo-agents", "kg", "claude-entities")
MODULE = "evo_agents.kg.claude_code"
BINARY_ENV = "EVO_KG_CLAUDE"  # the claude executable, when it is not ``claude`` on PATH
KEY_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")  # never reach Claude Code: the login decides
LOGIN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"  # the lease of the pool's account in a run
RUN_VARIABLE = "EVO_RUN_ID"  # set by the worker in a run's environment
STOP_EXIT = 75  # EX_TEMPFAIL: the step stops at this item, a later build goes on
TIMEOUT = 280  # seconds; the step gives a command 300
ERROR_CHARS = 200
SYSTEM_PROMPT = "You extract entities and relations from a text and answer with one JSON object, nothing else."
LOGIN = re.compile(
    r"/login|not logged in|log ?in again|invalid api key|oauth token|authenticat|unauthori[sz]ed|\b401\b", re.I
)
LIMIT = re.compile(r"usage limit|limit reached|rate.?limit|too many requests|\b429\b|overloaded|\b529\b", re.I)
SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+)")


class ClaudeError(RuntimeError):
    """A call that failed for a reason a later call does not undo."""


class ClaudeStop(ClaudeError):
    """The login is missing or refused, or a limit was reached: stop here, go on later. ``kind`` is login or
    rate_limit."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def uses(command) -> bool:
    """Whether the argv ``command`` runs this command (or ``claude`` itself): a mapping that must name a sink."""
    if not isinstance(command, (list, tuple)) or not command:
        return False
    argv = [str(a) for a in command]
    if os.path.basename(argv[0]) == "claude":
        return True
    for i in range(len(argv) - 1):
        if argv[i] == "kg" and argv[i + 1] == COMMAND[2]:
            return True
        if argv[i] == "-m" and argv[i + 1] == MODULE:
            return True
    return False


def binary(env: Mapping[str, str] | None = None) -> str | None:
    """The claude executable of this machine; None without one."""
    env = os.environ if env is None else env
    named = env.get(BINARY_ENV)
    if named:
        return named if os.path.isfile(named) and os.access(named, os.X_OK) else None
    return shutil.which("claude", path=env.get("PATH"))


def available(env: Mapping[str, str] | None = None) -> bool:
    return binary(env) is not None


def _masked(text: str) -> str:
    return SECRET.sub("[masked]", str(text))[:ERROR_CHARS]


def run_login(env: Mapping[str, str]) -> dict[str, str]:
    """The login of the run this process belongs to, when ``env`` lacks it: Claude Code keeps CLAUDE_CODE_OAUTH_TOKEN
    out of the shell of its tools, so ``evo-agents hub kg entities`` run by a run's agent would start ``claude``
    logged out. The run's socket gives it back (``op env``, as ``evo-agents worker env --run N`` does), and only that
    variable is taken. {} outside a run, with the variable set already, or when the socket answers nothing."""
    raw = str(env.get(RUN_VARIABLE) or "").strip()
    if env.get(LOGIN_VARIABLE) or not raw.isdigit():
        return {}
    try:
        from evo_agents.worker import credential_commands

        leases = credential_commands.lease_env(int(raw), environ=env) or {}
    except (AttributeError, OSError):  # no AF_UNIX socket on this platform, or no worker home to read
        return {}
    value = leases.get(LOGIN_VARIABLE)
    return {LOGIN_VARIABLE: value} if isinstance(value, str) and value else {}


def with_run_login(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` with the run's login added when it lacks one (``run_login``)."""
    return {**env, **run_login(env)}


def _environment(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in with_run_login(env).items() if k not in KEY_VARIABLES}


def argv(claude: str, model: str, system: str = SYSTEM_PROMPT) -> list[str]:
    return [
        claude,
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--max-turns",
        "1",
        "--system-prompt",
        system,
    ]


def _usage(result: Mapping) -> dict:
    used = result.get("usage") if isinstance(result.get("usage"), Mapping) else {}

    def number(key):
        value = used.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    usage = {
        "input_tokens": number("input_tokens")
        + number("cache_creation_input_tokens")
        + number("cache_read_input_tokens"),
        "output_tokens": number("output_tokens"),
    }
    cost = result.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        usage["cost"] = float(cost)
    return usage


def _refusal(text: str) -> ClaudeStop | None:
    if LOGIN.search(text):
        return ClaudeStop("login", f"Claude Code has no usable login: {_masked(text)}")
    if LIMIT.search(text):
        return ClaudeStop("rate_limit", f"Claude Code reached a limit: {_masked(text)}")
    return None


def answer(
    request: Mapping,
    *,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """The model's answer to ``request`` with its ``usage``. ClaudeStop or ClaudeError when the call fails."""
    from evo_agents.kg import entity_merge
    from evo_agents.kg.entity_mapping import EntitiesError, parse_answer, prompt

    env = os.environ if env is None else env
    model = request.get("model")
    if not isinstance(model, str) or not model:
        raise ClaudeError("the request names no model")
    text = request.get("text")
    if not isinstance(text, str):
        raise ClaudeError("the request has no text")
    kinds = [k for k in request.get("node_kinds") or [] if isinstance(k, str)]
    relations = [r for r in request.get("relations") or [] if isinstance(r, str)]
    claude = binary(env)
    if claude is None:
        raise ClaudeError("claude is not installed on this machine (install Claude Code and run claude login)")
    task = request.get("task")
    if task is not None and task not in entity_merge.TASKS:
        raise ClaudeError(f"the request asks for an unknown task {str(task)[:40]!r}")
    try:
        done = runner(
            argv(claude, model, entity_merge.SYSTEM if task else SYSTEM_PROMPT),
            input=text if task else prompt(text, kinds, relations, request.get("style") or "closed"),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
            env=_environment(env),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClaudeError(f"claude did not run: {type(exc).__name__}") from None
    try:
        result = json.loads(done.stdout or "")
    except ValueError:
        result = None
    if not isinstance(result, dict):
        said = f"{done.stderr or ''} {done.stdout or ''}"
        raise _refusal(said) or ClaudeError(f"claude exited {done.returncode} without a result: {_masked(said)}")
    said = str(result.get("result") or "")
    if result.get("is_error") or done.returncode != 0 or result.get("subtype") not in (None, "success"):
        why = f"{said} {result.get('subtype') or ''} {done.stderr or ''}"
        raise _refusal(why) or ClaudeError(f"claude failed: {_masked(why)}")
    try:
        data = parse_answer(said)
    except EntitiesError as exc:
        raise ClaudeError(str(exc)) from None
    if task:
        return {**{k: data[k] for k in entity_merge.ANSWER_KEYS if k in data}, "usage": _usage(result)}
    return {"nodes": data.get("nodes") or [], "edges": data.get("edges") or [], "usage": _usage(result)}


def main(argv_: Sequence[str] | None = None, *, stdin=None, stdout=None) -> int:
    """The command: one request on stdin, one answer on stdout. 0, STOP_EXIT or 1."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        request = json.loads(stdin.read())
        if not isinstance(request, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        print(json.dumps({"error": f"the request is not a JSON object: {exc}"}), file=stdout)
        return 1
    try:
        got = answer(request)
    except ClaudeStop as stop:
        print(json.dumps({"stop": stop.kind, "error": str(stop)}), file=stdout)
        return STOP_EXIT
    except ClaudeError as exc:
        print(json.dumps({"error": _masked(str(exc))}), file=stdout)
        return 1
    print(json.dumps(got, ensure_ascii=False), file=stdout)
    return 0


def cmd_claude_entities(args) -> int:
    return main()


def register(ksub) -> None:
    p = ksub.add_parser(
        COMMAND[2],
        help="the entities step's model command: one request on stdin, Claude Code headless, one answer on stdout",
    )
    p.set_defaults(func=cmd_claude_entities)


if __name__ == "__main__":
    raise SystemExit(main())
