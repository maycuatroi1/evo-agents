"""Claude Code through ``claude-agent-sdk``: a ``ClaudeSDKClient`` in streaming mode, never the CLI's stdout parsed
here.

- The session id is the daemon's: a new UUID for a new run (``session_id``), the run's session when it goes on with
  one (``resume``), so the daemon reports it from the start and a takeover resumes it with ``claude --resume``.
- The agent works in the worktree (``cwd``) with ``permission_mode="bypassPermissions"``, the SDK's form of
  ``--dangerously-skip-permissions``, and the preset system prompt of Claude Code with HEADLESS_NOTE, BACKGROUND_NOTE
  and SUBAGENT (a fresh subagent with a plan handoff) appended (``--append-system-prompt``). The owner's settings,
  hooks, plugins and CLAUDE.md load as for ``claude`` itself.
- The agent packs of the run's profile (``context.packs``, ``evo_agents.worker.packs``) load as local plugins
  (``plugins``, ``--plugin-dir``), with their skills, subagents and hooks; their MCP servers go in ``mcp_servers`` by
  their own names, and their instructions after the profile's in the appended system prompt.
- The system prompt holds nothing of the machine, the worktree or the step, so a new session reads it from the cache a
  session in another worktree, run or step wrote: the preset leaves its dynamic sections out
  (``exclude_dynamic_sections``, sent in the SDK's initialize request; the CLI's
  ``--exclude-dynamic-system-prompt-sections``). Claude Code 2.1.296 then moves its auto memory section, whose path
  names the worktree, into the first user message, which already holds CLAUDE.md, the git status snapshot and the
  prompt; the working directory and the platform come in the context it sends after the prompt. Measured on 2026-10-18
  with one-turn sessions: the first request in a second worktree read 11,504 tokens from the cache instead of 10,488,
  the tools and the system prompt; the rest, from the first user message on, was written anew, even in the same
  worktree with a prompt that differed only in its last words.
- The input stream the client is connected with stays open while the agent may take input: the prompt and each of
  the owner's messages go in with ``query()``. Claude Code takes a message that arrives mid-turn at the next tool
  boundary, and may fold it into the same ``result`` (research_notes/worker-runtimes.md of the harness).
- Background commands, as Claude Code 2.1.291 with claude-agent-sdk 0.2.163 reports them (tried with
  ``sleep 20 && date -u`` and ``run_in_background: true`` in streaming input, the agent told to end its turn once the
  command started): the Bash ``tool_use`` carries ``run_in_background: true``; the CLI answers at once with
  ``system/background_tasks_changed`` (the background tasks running, ``task_id`` and ``description`` each),
  ``system/task_started`` (``is_backgrounded: true``, ``task_id``, ``tool_use_id``) and the tool's result
  (``tool_use_result.backgroundTaskId``, "Command running in background with ID: ..."), and the turn goes on to its
  ``result``. When the command ends, 20 seconds later, the CLI sends ``background_tasks_changed`` without it,
  ``task_updated`` (``patch.status`` ``completed``) and ``task_notification`` (``status``, ``summary``,
  ``output_file``, ``tool_use_id``), then opens a turn by itself while its input is open: a new ``system/init``, the
  agent reads the output file, and a second ``result`` whose ``origin`` is ``{"kind": "task-notification"}``. Nobody
  has to write to it. When its input ends before that, as it did at the first ``result`` before this adapter waited,
  the CLI kills the command within 5 seconds (``task_updated`` ``killed``, ``task_notification`` ``stopped``) and
  exits, so the agent never learns how the command ended. A command in the foreground also has ``task_started``
  (``is_backgrounded: false``) and ``task_notification``, but no ``background_tasks_changed`` and no
  ``backgroundTaskId``.
- At a ``result``, the adapter ends the input stream (and the CLI exits once it has done what it was given) unless the
  agent may go on in this session: when a command it started in the background still runs (``Background`` follows the
  Bash calls with ``run_in_background``, their results, and the tasks the CLI reports running in the background until
  it reports their end), the input stays open and the CLI's own turn after the command ends goes on with the agent.
  Should no turn open within ``follow_up_after`` seconds of that end, the adapter writes to the agent itself
  (FOLLOW_UP, with the CLI's summary and output file of each command). After a turn in which a background command
  ended, the CLI has ``turn_grace`` seconds to open a turn for its notification. A command that still runs
  ``background_wait`` seconds after the turn ended is left: the input ends, and the CLI stops it and exits. Once the
  agent has written ``.evo-run/result.json`` it has finished, and its ``result`` ends the input whatever still runs.
  A decision of a plan run left open is the daemon's, not the adapter's: the turn ends, and the daemon waits for the
  answer and hands it to a new adapter in the same session (``evo_agents.worker.adapter``).
- ``stop_at_turn_boundary`` ends the input stream at once, so the turn in progress finishes and the CLI exits, a
  background command or not; ``interrupt`` sends the SDK's interrupt, which stops the turn and its tools, then ends
  it.
- Whether the turn completed comes from the last ``result`` (``subtype`` ``success``, not ``is_error``), never from
  the exit code: Claude Code exits 0 when it is interrupted.
- The run's usage adds up the ``result`` of each turn, with the session's ``total_cost_usd``, and holds
  ``peak_context_tokens``: the largest context a message of the model read in the main thread, its usage's input,
  cache creation and cache read tokens, as the stream's ``assistant`` messages carry it (``context_of``); and the
  cache read and cache creation tokens of the first of those messages, the agent's first request
  (``first_request_of``, ``common.FIRST_REQUEST``).
- The CLI is ``claude`` on PATH, started through the launcher of ``common`` (a session of its own).
- Claude Code authenticates with the machine's own login, unless the environment says otherwise. A lease of the run
  may set CLAUDE_CODE_OAUTH_TOKEN, a token of ``claude setup-token`` (docs/credentials.md). Claude Code takes
  ANTHROPIC_API_KEY before that token, so when no lease sets the key too, the key of the daemon's environment is left
  out of the agent's: the launcher unsets it (the SDK hands the CLI its own process's environment under
  ``options.env``), and the daemon notes it in the run's log (``environment_notes``). Such a token only calls the
  model and cannot open a Remote Control session, so the terminal UI then starts without ``--remote-control``
  (``evo_agents.worker.interactive``).
- A run of the Curator (``context.run["curator"]``) uses the Claude subscription login alone, never an API key (plan
  decision 14 of the curator-agent plan): ANTHROPIC_API_KEY is left out of its agent's environment and unset by the
  launcher whatever leases it, and ``login_refusal`` refuses to start it on a machine where Claude Code has no
  subscription login for it (``subscription_login``: CLAUDE_CODE_OAUTH_TOKEN in the run's environment, the
  credentials file of Claude Code's configuration directory, or on macOS its keychain item).
- A run the night shift queued has a budget (``evo_agents.hub.curator``): ``max_budget_usd`` is what is left of its
  ``max_usd`` once the session it goes on in has cost ``context.spent_usd`` (the CLI counts only the spend of its own
  process against it, while its ``total_cost_usd`` goes on from the total the session's transcript saved), and
  ``max_turns`` its ``max_turns``. A ``result`` of ``error_max_budget_usd`` or ``error_max_turns`` ends the run with
  an outcome that names the cap, ``cost`` or ``turns``.
- An author run (``context.run["kind"]`` ``author``, ``evo_agents.hub.author``) and a chat run (``chat``,
  ``evo_agents.hub.chat_runs``) have the tools that ask the person at the terminal a question turned off
  (``disallowed_tools``, the CLI's ``--disallowedTools``: ``AskUserQuestion``): nobody at the worker's machine answers
  it, and the agent asks in its last message instead. A chat run's prompt is delivered verbatim
  (``verbatim_prompts``): an ``@name`` or ``@path`` the chat's people wrote never makes Claude Code read a file.
- The agent compacts its context as it nears its auto-compact window: the run's environment
  (``evo_agents.worker.runner.agent``, so ``options.env`` and the terminal UI's) gets CLAUDE_CODE_AUTO_COMPACT_WINDOW
  from ``compact_window``, which keeps the daemon's own, else takes EVO_WORKER_CLAUDE_CODE_AUTO_COMPACT_WINDOW
  (``off`` or ``0`` leaves Claude Code's window), else AUTO_COMPACT_WINDOW, 200,000. The sessions of worker runs on the
  laptop (33 by 2026-10-11) peaked at a median of 41,950 tokens of context; the six longest, runs of 105 to 188 model
  calls, at 219,000 to 297,000, re-reading 17 to 31 million cached tokens each, and none compacted (docs/workers.md).
- The model is the run's ``model`` (``options.model``, the CLI's ``--model``), else ``EVO_WORKER_CLAUDE_CODE_MODEL``,
  else Claude Code's own choice. Claude Code has no command that lists its models; for the heartbeat, ``models``
  gives the aliases its ``--model`` help names (``'opus'``, ``'sonnet'`` and the like), and a run may name any model
  the CLI takes.
- A turn that ends with an error that reads as the account's usage limit (``limit_of_text``) gives an outcome with
  ``limit`` set, and its ``rate_limit_event`` messages reach the run's log raw: ``evo_agents.worker.runner.limits``
  reports them and moves the run to another account. One whose error reads as the account failing to authenticate
  (``auth_failure_of``: "Failed to authenticate. API Error: 401 OAuth access token is invalid.") gives an outcome with
  ``auth`` set, which ``limits`` reports and moves the same way. A result whose subtype is ``success`` but is an error
  is said as "ended its turn with an error", never "with success".
- A run under an agent profile that limits tool, skill or model, or has instructions, gets them in the session as
  ``evo_agents.worker.runtimes.profiles`` says: ``tools``, ``skills`` and ``setting_sources``, ``allowedMcpServers``
  and ``availableModels`` in ``settings``, a PreToolUse hook, the model, and the instructions after SYSTEM_NOTE. With a
  tool allowlist, the adapter asks the CLI for its MCP servers once it is connected, before the prompt, and raises
  ``ProfileRefused`` on one the profile leaves out (the owner's own settings may allow more).

Checked with Claude Code 2.1.289 and claude-agent-sdk 0.2.163 (``evo-agents worker selftest --runtime claude-code``);
the background commands with Claude Code 2.1.291 and the same SDK (``evo-agents worker selftest --runtime claude-code
--background``).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.hub import author, curator, digest, runs
from evo_agents.worker import packs
from evo_agents.worker.adapter import AgentEvent, Detection, Outcome, ProfileRefused, RunContext, command_output
from evo_agents.worker.runtimes import profiles
from evo_agents.worker.runtimes.common import (
    HEADLESS_NOTE,
    INTERRUPT_TIMEOUT,
    SUBAGENT_NOTE,
    AgentFinished,
    QueueAdapter,
    add_numbers,
    cut,
    detect_runtime,
    first_request,
    larger,
    message_chunk,
    plan,
    raw,
    run_setting,
    thought_chunk,
    tool_call,
    tool_update,
    usage_update,
    with_peak,
    write_launcher,
)

log = logging.getLogger("evo_agents.worker")

MIN_VERSION = "2.1.289"  # Claude Code
SDK = ("claude-agent-sdk", "0.2.163")
# Variables of a parent Claude Code session that must not reach the agent, which is no child of it.
DROPPED_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
# Claude Code's credentials in the agent's environment: a token of `claude setup-token`, and the API key that comes
# before it in Claude Code's order of authentication.
OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY = "ANTHROPIC_API_KEY"
API_KEY_NOTE = (
    f"{API_KEY} of this machine's environment is left out of Claude Code's: the run's lease sets {OAUTH_TOKEN}, and "
    "Claude Code would take the API key before that token."
)
REMOTE_CONTROL_NOTE = (
    f"Claude Code's terminal UI starts without --remote-control: the run's lease sets {OAUTH_TOKEN}, a token of "
    "`claude setup-token`, which only calls the model and cannot open a Remote Control session, so the session does "
    "not show in the Claude apps."
)
# System messages left out of the log: the CLI's list of slash commands (tens of KiB, twice a session), the start of
# a hook (its response says the same and how it ended), and running estimates of thinking tokens.
SKIPPED_SYSTEM = frozenset({"commands_changed", "hook_started", "thinking_tokens"})
# The --model option of `claude --help`, up to the next option, and the aliases its text quotes.
MODEL_HELP = re.compile(r"^\s*--model[ =<].*?(?=^\s*-|\Z)", re.M | re.S)
MODEL_ALIAS = re.compile(r"'([A-Za-z][A-Za-z0-9._\[\]-]*)'")
# Seconds the session stays open, once a turn has ended, while a command the agent started in the background still
# runs; then the input ends, and the CLI stops the command and exits.
BACKGROUND_WAIT = 30 * 60.0
# Seconds the CLI has to open a turn by itself once a background command ended while no turn ran, before the adapter
# writes to the agent (FOLLOW_UP). Claude Code 2.1.291 opens it at once.
FOLLOW_UP_AFTER = 15.0
# Seconds the CLI has to open a turn after one in which a background command ended: its notification may have reached
# the agent in that turn, or wait for the next.
TURN_GRACE = 5.0
# The results of a session the run's budget stopped, and the cap each one names (Outcome.cap).
CAP_RESULTS = {"error_max_budget_usd": "cost", "error_max_turns": "turns"}
# The text of a turn that ended on the account's usage limit, as Claude Code words it: "Claude AI usage limit
# reached|1791507000" (the reset as a unix time), "5-hour limit reached ∙ resets 3pm", "You've hit your limit".
USAGE_LIMIT = re.compile(r"usage limit|hit your (?:usage )?limit|\blimit reached\b|out of extra usage", re.I)
RESET_STAMP = re.compile(r"\|\s*(\d{9,11})\b")
# The text of a turn that ended because the API refused the account's token, as Claude Code words it: "Failed to
# authenticate. API Error: 401 OAuth access token is invalid." (2.1, run #58 of 2026-10-14), "OAuth token has expired".
AUTH_FAILURE = re.compile(
    r"failed to authenticate|api error: 401\b|oauth (?:access )?token (?:is invalid|has expired|has been revoked)"
    r"|authentication_error|invalid api key",
    re.I,
)
AUTH_CHARS = 300  # of the error kept as the reason
# The statuses of a task that has ended: task_notification says stopped, task_updated killed, for the same end.
TERMINAL_TASK = frozenset({"completed", "failed", "stopped", "killed"})
# The id of a background command in the text of its Bash result.
BACKGROUND_ID = re.compile(r"running in background with ID: ([A-Za-z0-9_-]+)")
# Appended to the system prompt after HEADLESS_NOTE.
BACKGROUND_NOTE = (
    "A command you start with run_in_background keeps this session open after your turn ends: you are told when it "
    "ends, in a new turn, and go on from there. Stop a server or watcher you started in the background once you no "
    "longer need it, or the session waits for it."
)
# Claude Code's Task tool starts a subagent with its own context; the note keeps that and adds the plan handoff.
_CLAUDE_FORK = "With the Task tool, start a fresh subagent, not a resumed or forked one."
SUBAGENT = SUBAGENT_NOTE.format(fork=_CLAUDE_FORK)
SYSTEM_NOTE = f"{HEADLESS_NOTE}\n\n{BACKGROUND_NOTE}\n\n{SUBAGENT}"
# What the agent is told when its background commands ended and the CLI opened no turn for them.
FOLLOW_UP = (
    "The command(s) you started in the background have ended:\n{ended}\nRead their output and go on with the task "
    "from where you stopped."
)
TOOL_KINDS = {
    "Read": "read",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Write": "edit",
    "NotebookEdit": "edit",
    "Bash": "execute",
    "BashOutput": "execute",
    "KillShell": "execute",
    "Grep": "search",
    "Glob": "search",
    "WebFetch": "fetch",
    "WebSearch": "fetch",
    "Task": "think",
    "TodoWrite": "think",
}


# The auto-compact window of a run's agent, in tokens, and the variables that set it (``compact_window``).
AUTO_COMPACT = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
AUTO_COMPACT_SETTING = "EVO_WORKER_CLAUDE_CODE_AUTO_COMPACT_WINDOW"
AUTO_COMPACT_WINDOW = 200_000


CREDENTIALS_FILE = ".credentials.json"  # where Claude Code keeps its login on Linux, in its configuration directory
KEYCHAIN_ITEM = "Claude Code-credentials"  # and on macOS
KEYCHAIN_TIMEOUT = 10.0


def curator_run(context: RunContext) -> bool:
    """Whether the run is a run of the Curator, which uses the Claude subscription login alone."""
    return isinstance(context.run.get("curator"), dict)


def _keychain_item() -> bool:
    """Whether macOS keeps Claude Code's login in the keychain: the item's attributes only, never its secret."""
    if sys.platform != "darwin":
        return False
    try:
        done = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_ITEM],
            capture_output=True,
            timeout=KEYCHAIN_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def subscription_login(env: Mapping[str, str], *, keychain: Callable[[], bool] | None = None) -> str | None:
    """Where Claude Code finds a Claude subscription login for an agent started with ``env``: CLAUDE_CODE_OAUTH_TOKEN
    in it, the credentials file of its configuration directory (CLAUDE_CONFIG_DIR, else ~/.claude), or the macOS
    keychain item (``keychain`` asks for it; tests hand their own); None when there is none."""
    if env.get(OAUTH_TOKEN):
        return f"{OAUTH_TOKEN} in the run's environment"
    configured = env.get("CLAUDE_CONFIG_DIR") or ""
    home = env.get("HOME") or os.path.expanduser("~")
    directory = Path(configured) if os.path.isabs(configured) else Path(home) / ".claude"
    if (directory / CREDENTIALS_FILE).is_file():
        return str(directory / CREDENTIALS_FILE)
    if (keychain or _keychain_item)():
        return f"the keychain item {KEYCHAIN_ITEM}"
    return None


def curator_login_refusal(context: RunContext, *, keychain: Callable[[], bool] | None = None) -> str | None:
    """Why a run of the Curator may not start on Claude Code here: it has no subscription login (plan decision 14);
    None for any other run, and for one that has it."""
    if not curator_run(context) or subscription_login(context.env, keychain=keychain) is not None:
        return None
    return (
        "a run of the Curator on Claude Code uses the Claude subscription login alone, never ANTHROPIC_API_KEY (plan "
        f"decision 14), and this machine has none for it: no {OAUTH_TOKEN} in the run's environment, no "
        f"{CREDENTIALS_FILE} in Claude Code's configuration directory, no keychain item {KEYCHAIN_ITEM}. Sign Claude "
        f"Code in with the subscription on the worker, or give the charter's env_secrets a secret {OAUTH_TOKEN} made "
        "by `claude setup-token`"
    )


def leased_oauth(context: RunContext) -> bool:
    """Whether a lease of the run sets CLAUDE_CODE_OAUTH_TOKEN in the agent's environment."""
    return OAUTH_TOKEN in context.leased and bool(context.env.get(OAUTH_TOKEN))


def drops_api_key(context: RunContext) -> bool:
    """Whether ANTHROPIC_API_KEY stays out of Claude Code's environment: a run of the Curator, which uses the Claude
    subscription login alone; or a lease sets CLAUDE_CODE_OAUTH_TOKEN and none sets the key, which Claude Code would
    take first."""
    return curator_run(context) or (leased_oauth(context) and API_KEY not in context.leased)


def dropped_env(context: RunContext) -> tuple[str, ...]:
    """The variables of ``context.env`` Claude Code does not get: DROPPED_ENV, and ANTHROPIC_API_KEY when
    ``drops_api_key``."""
    return (*DROPPED_ENV, API_KEY) if drops_api_key(context) else DROPPED_ENV


def auth_notes(context: RunContext) -> list[str]:
    """API_KEY_NOTE when the daemon's ANTHROPIC_API_KEY is left out of the agent's environment for a leased token."""
    leased = leased_oauth(context) and API_KEY not in context.leased
    return [API_KEY_NOTE] if leased and API_KEY in context.env else []


def compact_window(env: Mapping[str, str]) -> dict[str, str]:
    """What the agent's environment gains for its auto-compact window: nothing when the daemon's environment sets
    CLAUDE_CODE_AUTO_COMPACT_WINDOW itself or EVO_WORKER_CLAUDE_CODE_AUTO_COMPACT_WINDOW is ``off`` or ``0``; else that
    setting when it is a whole number of tokens, else AUTO_COMPACT_WINDOW. Claude Code takes 100,000 to 1,000,000 and
    caps it at the model's context window."""
    if env.get(AUTO_COMPACT, "").strip():
        return {}
    value = env.get(AUTO_COMPACT_SETTING, "").strip().lower()
    if value in ("off", "0"):
        return {}
    return {AUTO_COMPACT: value if value.isdigit() else str(AUTO_COMPACT_WINDOW)}


def help_models(text: str | None) -> list[str] | None:
    """The aliases the ``--model`` option of ``claude --help`` quotes, in order; None when it quotes none."""
    found = MODEL_HELP.search(text or "")
    if found is None:
        return None
    return MODEL_ALIAS.findall(found.group(0)) or None


def _text_of(content) -> str | None:
    """The text of a tool result's content: a string, or a list of content blocks."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item.get("text") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if parts:
            return "\n".join(parts)
    return str(content)


def _true(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


class Background:
    """The commands the agent runs in the background, from what Claude Code reports of them (the module's docstring):
    each Bash call with ``run_in_background`` until its result names its task or says it did not start, and each task
    the CLI reports running in the background until it reports its end."""

    def __init__(self) -> None:
        self.calls: dict[str, str | None] = {}  # tool_use_id -> task id; None until the CLI names the task
        self.running: dict[str, str] = {}  # task id -> description, of the tasks running in the background
        self.ended: dict[str, dict] = {}  # task id -> {description, status, summary, output_file} as the CLI told

    @property
    def pending(self) -> bool:
        """Whether a command the agent started in the background may still run."""
        return bool(self.calls) or bool(self.running)

    def see(self, message) -> list[str]:
        """Follow ``message`` of the SDK; the ids of the background tasks whose end it reports first."""
        from claude_agent_sdk import types as sdk

        if isinstance(message, sdk.AssistantMessage):
            if message.parent_tool_use_id is None:  # a subagent's commands are the subagent's
                for block in message.content:
                    if (
                        isinstance(block, sdk.ToolUseBlock)
                        and block.name == "Bash"
                        and isinstance(block.input, dict)
                        and _true(block.input.get("run_in_background"))
                    ):
                        self.calls[block.id] = None
        elif isinstance(message, sdk.UserMessage):
            self._tool_results(message)
        elif isinstance(message, sdk.SystemMessage) and isinstance(message.data, dict):
            return self._system(message.subtype, message.data)
        return []

    def _tool_results(self, message) -> None:
        from claude_agent_sdk import types as sdk

        if not isinstance(message.content, list):
            return
        blocks = [block for block in message.content if isinstance(block, sdk.ToolResultBlock)]
        extra = message.tool_use_result if isinstance(message.tool_use_result, dict) else {}
        for block in blocks:
            task = extra.get("backgroundTaskId") if len(blocks) == 1 else None
            task = task if isinstance(task, str) and task else None
            if block.tool_use_id in self.calls:
                if task is None:
                    found = BACKGROUND_ID.search(_text_of(block.content) or "")
                    task = found.group(1) if found else None
                if block.is_error or task is None:  # it did not start, or ran in the foreground after all
                    del self.calls[block.tool_use_id]
                    continue
                self.calls[block.tool_use_id] = task
            if task is not None and not block.is_error:  # a command the CLI moved to the background, too
                self._start(task, None)

    def _system(self, subtype: str, data: dict) -> list[str]:
        task = data.get("task_id")
        task = task if isinstance(task, str) and task else None
        if subtype == "task_started":
            if task is not None and data.get("is_backgrounded") is True:
                if data.get("tool_use_id") in self.calls:
                    self.calls[data["tool_use_id"]] = task
                self._start(task, data.get("description"))
            return []
        if subtype == "background_tasks_changed":
            listed: dict[str, str | None] = {}
            for item in data.get("tasks") or []:
                if isinstance(item, dict) and isinstance(item.get("task_id"), str) and item["task_id"]:
                    listed[item["task_id"]] = item.get("description")
            for name, description in listed.items():
                self._start(name, description)
            return [name for name in list(self.running) if name not in listed and self._end(name, {})]
        if subtype == "task_notification" and task is not None and data.get("status") in TERMINAL_TASK:
            info = {key: data[key] for key in ("status", "summary", "output_file") if data.get(key)}
            return [task] if self._end(task, info, data.get("tool_use_id")) else []
        if subtype == "task_updated" and task is not None:
            patch = data.get("patch") if isinstance(data.get("patch"), dict) else {}
            if patch.get("status") in TERMINAL_TASK:
                return [task] if self._end(task, {"status": patch["status"]}) else []
        return []

    def _start(self, task: str, description) -> None:
        if task in self.ended:
            return
        known = self.running.get(task)
        self.running[task] = description if isinstance(description, str) and description else known or ""

    def _end(self, task: str, info: dict, tool_use_id=None) -> bool:
        """Note the end of ``task``; whether it is the first news of the end of a background command."""
        tracked = task in self.running or task in self.calls.values() or tool_use_id in self.calls
        description = self.running.pop(task, None)
        for call, known in list(self.calls.items()):
            if known == task or call == tool_use_id:
                del self.calls[call]
        if task in self.ended:
            self.ended[task].update(info)
            return False
        if not tracked:  # a command of the foreground, whose task ends with it
            return False
        self.ended[task] = {"description": description or "", **info}
        return True

    def describe(self, task: str) -> str:
        """One line on a background task that ended, for FOLLOW_UP."""
        info = self.ended.get(task, {})
        line = f"- {info.get('summary') or info.get('description') or task} ({info.get('status') or 'ended'})"
        if info.get("output_file"):
            line += f"; its output is in {info['output_file']}"
        return line


def _opens_turn(message) -> bool:
    """Whether ``message`` comes from a turn: the CLI's init at the start of one, or the agent's own message."""
    from claude_agent_sdk import types as sdk

    if isinstance(message, (sdk.AssistantMessage, sdk.StreamEvent)):
        return True
    return isinstance(message, sdk.SystemMessage) and message.subtype == "init"


def unix_time(value) -> str | None:
    """A unix time of the CLI as ISO 8601 in UTC; None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def limit_of_text(text: str | None) -> dict | None:
    """The limit an error of the agent's turn names, when it reads as the account's usage limit: ``{status:
    rejected}``, with resets_at when the text carries the reset as a unix time; None otherwise."""
    if not text or not USAGE_LIMIT.search(text):
        return None
    found = RESET_STAMP.search(text)
    resets_at = unix_time(int(found[1])) if found else None
    return {"status": "rejected", **({"resets_at": resets_at} if resets_at else {})}


def auth_failure_of(text: str | None) -> str | None:
    """The error of the agent's turn, on one line, when it reads as its account failing to authenticate (the API
    refused its token); None otherwise."""
    if not text or not AUTH_FAILURE.search(text):
        return None
    return " ".join(text.split())[:AUTH_CHARS]


def _raw_of(message) -> dict:
    """A message of the SDK as the CLI wrote it, as far as the SDK keeps it."""
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        return data
    info = getattr(message, "rate_limit_info", None)
    if info is not None:
        return {"type": "rate_limit_event", "rate_limit_info": getattr(info, "raw", None) or {}}
    if dataclasses.is_dataclass(message):
        return {"type": type(message).__name__, **dataclasses.asdict(message)}
    return {"type": type(message).__name__}


def context_of(message) -> int | None:
    """The tokens of the context a message of the model read, as its usage counts them (``digest.context_tokens``:
    input, cache creation and cache read); None for any other message, one of a subagent (``parent_tool_use_id``),
    whose context is its own, and one no model sent."""
    from claude_agent_sdk import types as sdk

    if not isinstance(message, sdk.AssistantMessage) or message.parent_tool_use_id is not None:
        return None
    if message.model == digest.SYNTHETIC_MODEL:
        return None
    return digest.context_tokens(getattr(message, "usage", None))


def first_request_of(message) -> dict | None:
    """The FIRST_REQUEST keys of the run's usage from a message ``context_of`` reads, the first of which is the agent's
    first request: the tokens it read from the cache and wrote to it. None for any other message."""
    if context_of(message) is None:
        return None
    usage = message.usage
    return first_request(usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"))


def events_of(message) -> list[AgentEvent]:
    """The hub's events for one message of ``ClaudeSDKClient.receive_messages()``."""
    from claude_agent_sdk import types as sdk

    events: list[AgentEvent] = []
    if isinstance(message, sdk.AssistantMessage):
        for block in message.content:
            if isinstance(block, sdk.TextBlock):
                if block.text:
                    events.append(message_chunk(block.text))
            elif isinstance(block, sdk.ThinkingBlock):
                if block.thinking.strip():  # thinking the API keeps to itself comes with an empty text
                    events.append(thought_chunk(block.thinking))
            elif isinstance(block, (sdk.ToolUseBlock, sdk.ServerToolUseBlock)):
                events.append(
                    tool_call(block.id, block.name, TOOL_KINDS.get(block.name, "other"), "pending", block.input)
                )
                todos = (
                    block.input.get("todos") if block.name == "TodoWrite" and isinstance(block.input, dict) else None
                )
                if isinstance(todos, list):
                    entries = [
                        (str(t.get("content") or ""), t.get("status"), "medium") for t in todos if isinstance(t, dict)
                    ]
                    events.append(plan(entries))
            elif isinstance(block, (sdk.ToolResultBlock, sdk.ServerToolResultBlock)):
                status = "failed" if getattr(block, "is_error", False) else "completed"
                events.append(tool_update(block.tool_use_id, status, _text_of(block.content)))
            else:
                events.append(raw({"type": "assistant", "block": dataclasses.asdict(block)}))
        if message.error:
            events.append(raw({"type": "assistant", "error": message.error}))
        return events
    if isinstance(message, sdk.UserMessage):
        # Tool results come back as user messages; the text of a user message is the prompt or the owner's, which
        # the hub has already.
        if isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, sdk.ToolResultBlock):
                    status = "failed" if block.is_error else "completed"
                    events.append(tool_update(block.tool_use_id, status, _text_of(block.content)))
        return events
    if isinstance(message, sdk.ResultMessage):
        extra = {"numTurns": message.num_turns, "durationMs": message.duration_ms}
        return [usage_update(message.usage or {}, message.total_cost_usd, **extra)]
    data = _raw_of(message)
    if data.get("type") == "system" and data.get("subtype") in SKIPPED_SYSTEM:
        return []
    return [raw(data)]


class ClaudeCodeAdapter(QueueAdapter):
    runtime = "claude-code"
    binary = "claude"
    interactive = True
    pack_kinds = packs.KINDS
    pack_transports = packs.TRANSPORTS
    background_wait = BACKGROUND_WAIT
    follow_up_after = FOLLOW_UP_AFTER
    turn_grace = TURN_GRACE

    @classmethod
    def detect(cls) -> Detection:
        return detect_runtime(cls.runtime, cls.binary, MIN_VERSION, packages=(SDK,))

    @classmethod
    def models(cls) -> list[str] | None:
        """The aliases ``claude --help`` names for ``--model``."""
        return help_models(command_output(cls.binary, "--help"))

    @classmethod
    def tui(cls, context: RunContext, session_id: str | None):
        from evo_agents.worker.interactive import ClaudeCodeTui

        return ClaudeCodeTui(context, session_id)

    @classmethod
    def environment_notes(cls, context: RunContext) -> list[str]:
        return auth_notes(context) + profiles.claude_notes(profiles.profile_of(context))

    @classmethod
    def profile_refusal(cls, run) -> str | None:
        return profiles.model_refusal(profiles.Profile.of(run), run)

    @classmethod
    def login_refusal(cls, context: RunContext) -> str | None:
        return curator_login_refusal(context)

    def __init__(self, context: RunContext):
        super().__init__(context)
        self._session = context.resume_session or str(uuid.uuid4())
        self._client = None
        self._input_open = False
        self._end_input = asyncio.Event()
        self._results: list = []
        self._peak: int | None = None  # the largest context of a message of the model in the main thread
        self._first: dict | None = None  # the cache tokens of the first of those messages (FIRST_REQUEST)
        self._error: str | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._dir: Path | None = None
        self.options = None
        self.background = Background()
        self._turn_open = False  # a turn of the agent runs, or a message is on its way to start one
        self._ended_in_turn = False  # a background command ended during the turn that runs
        self._unseen: list[str] = []  # background tasks that ended while no turn ran
        self._timer: asyncio.Task | None = None

    @property
    def session_id(self) -> str | None:
        return self._session

    def make_client(self, options):
        """The SDK's client; a test hands it a transport of its own."""
        from claude_agent_sdk import ClaudeSDKClient

        return ClaudeSDKClient(options)

    def build_options(self, cli_path: str):
        from claude_agent_sdk import ClaudeAgentOptions

        dropped = dropped_env(self.context)
        profile = profiles.profile_of(self.context)
        append = packs.with_instructions(profiles.with_instructions(SYSTEM_NOTE, profile), self.context.packs)
        settings = {
            "cwd": str(self.context.worktree),
            "cli_path": cli_path,
            "permission_mode": "bypassPermissions",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                "append": append,
                "exclude_dynamic_sections": True,  # the same system prompt in every worktree (module docstring)
            },
            "env": {key: value for key, value in self.context.env.items() if key not in dropped},
            "stderr": self._stderr.append,
        }
        if self.context.resume_session:
            settings["resume"] = self._session
        else:
            settings["session_id"] = self._session
        for key, value in (
            ("model", profiles.session_model(self.context, self.runtime)),
            ("effort", run_setting(self.context, self.runtime, "effort")),
        ):
            if value:
                settings[key] = value
        limits = profiles.claude_settings(profile)
        disallowed = limits.pop("disallowed_tools", [])
        kind = self.context.run.get("kind")
        if kind in ("author", "chat", "task"):  # nobody at this machine answers a question tool
            disallowed = [*author.QUESTION_TOOLS, *disallowed]
        if disallowed:
            settings["disallowed_tools"] = disallowed
        if kind in ("chat", "task"):  # an @handle or @path in the chat's words or the task never makes it read a file
            settings["verbatim_prompts"] = True
        settings.update(limits)
        settings.update(packs.claude_options(self.context.packs))
        hooks = profiles.claude_hooks(profile)
        if hooks is not None:
            settings["hooks"] = hooks
        if self.budget is not None:
            usd = curator.usd_left(self.budget, self.context.spent_usd)
            if usd is not None:
                settings["max_budget_usd"] = usd
            if self.budget.get("max_turns"):
                settings["max_turns"] = int(self.budget["max_turns"])
        return ClaudeAgentOptions(**settings)

    async def _input(self):
        """The stream the client is connected with: it yields nothing, and its end makes the SDK close the CLI's
        stdin."""
        await self._end_input.wait()
        return
        yield {}  # an async generator

    async def _open(self) -> None:
        environment = self.context.environment
        program = environment.program(self.binary, self.context.env)
        self._dir = Path(tempfile.mkdtemp(prefix="evo-claude-"))
        self.pid_file = self._dir / "pid"
        unset = (API_KEY,) if drops_api_key(self.context) else ()  # the SDK passes on the daemon's own
        command = environment.command(program, context=self.context, directory=self._dir)
        self.options = self.build_options(str(write_launcher(self._dir, *command, unset=unset)))
        self._client = self.make_client(self.options)
        self._input_open = True
        await self._client.connect(prompt=self._input())
        await self._check_servers()
        self._turn_open = True
        await self._client.query(self.context.prompt)

    async def _check_servers(self) -> None:
        """ProfileRefused when the CLI has an MCP server the run's profile leaves out, which the session's
        ``allowedMcpServers`` does not keep out when the owner's own settings name more; nothing for a profile that
        allows every tool."""
        profile = profiles.profile_of(self.context)
        if profile is None or profile.servers() is None:
            return
        try:
            status = await self._client.get_mcp_status()
        except Exception as exc:  # unchecked, the session might offer a server the profile leaves out
            why = f"claude-code did not list its MCP servers for agent profile {profile.label}: {exc}"
            raise ProfileRefused(why) from None
        extra = profiles.claude_extra_servers(profile, status)
        if extra:
            raise ProfileRefused(
                f"claude-code would offer the tools of MCP server(s) {', '.join(extra)}, which agent profile "
                f"{profile.label} leaves out (the worker's own Claude Code settings may allow more servers in "
                "allowedMcpServers): take them out of the worker's Claude Code configuration, or allow them in the "
                "profile"
            )

    async def _drive(self) -> None:
        from claude_agent_sdk import types as sdk

        try:
            async for message in self._client.receive_messages():
                if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                    session = message.data.get("session_id")
                    if isinstance(session, str) and session:
                        self._session = session
                self.emit(events_of(message))
                self._peak = larger(self._peak, context_of(message))
                self._first = self._first or first_request_of(message)
                ended = self.background.see(message)
                if isinstance(message, sdk.ResultMessage):
                    self._results.append(message)
                    await self._turn_ended()
                    continue
                if not self._turn_open and _opens_turn(message):  # the CLI opened a turn by itself
                    self._turn_starts()
                if ended:
                    self._background_ended(ended)
        except Exception as exc:  # the CLI died, or wrote what the SDK cannot read
            self._error = f"{type(exc).__name__}: {exc}"
        self.outcome = self._outcome()

    def _outcome(self) -> Outcome:
        usage: dict = {}
        for result in self._results:
            add_numbers(usage, result.usage)
        if self._results and self._results[-1].total_cost_usd is not None:
            usage["total_cost_usd"] = self._results[-1].total_cost_usd  # the session's, counted by the CLI
        usage = with_peak(usage, self._peak, self._first)
        if not self._results:
            why = self._error or "the CLI ended without finishing its turn"
            tail = " | ".join(list(self._stderr)[-3:])
            if tail and not self.interrupted:
                why += f" (stderr: {tail})"
            if self.interrupted:
                return Outcome(False, "interrupted", usage)
            return Outcome(False, cut(f"claude-code: {why}"), usage, auth=auth_failure_of(why))
        last = self._results[-1]
        summary = last.result.strip() if isinstance(last.result, str) and last.result.strip() else None
        if self.interrupted:
            return Outcome(False, "interrupted", usage, summary)
        cap = CAP_RESULTS.get(last.subtype)
        if cap is not None:
            return Outcome(False, cut(self._cap_error(cap, last)), usage, summary, cap=cap)
        if last.is_error or last.subtype != "success":
            detail = str(last.result or ", ".join(str(error) for error in last.errors or []) or last.stop_reason or "")
            ended = "an error" if last.subtype == "success" else last.subtype  # is_error on a result of success
            error = cut(f"claude-code ended its turn with {ended}: {detail}")
            return Outcome(False, error, usage, summary, limit=limit_of_text(detail), auth=auth_failure_of(detail))
        return Outcome(True, None, usage, summary)

    def _cap_error(self, cap: str, last) -> str:
        """Why the run stopped, as its log and its error say it, for a result of CAP_RESULTS."""
        detail = "; ".join(str(error) for error in last.errors or []) or last.subtype
        budget = self.budget or {}
        if cap == "cost":
            limit = curator.money(budget["max_usd"]) if budget.get("max_usd") is not None else "its limit"
            spent = curator.money(last.total_cost_usd) if last.total_cost_usd is not None else "an unknown amount"
            return f"claude-code stopped at the run's cost cap of {limit}: the session cost {spent} ({detail})"
        turns = int(budget["max_turns"]) if budget.get("max_turns") else "its"
        return f"claude-code stopped at the run's cap of {turns} turns ({detail})"

    # The end of a turn, and what the session waits for after it

    def _turn_starts(self) -> None:
        self._turn_open = True
        self._ended_in_turn = False
        self._unseen.clear()
        self._disarm()

    async def _turn_ended(self) -> None:
        """A ``result``: end the input, unless the agent may go on in this session (the module's docstring)."""
        self._turn_open = False
        ended_in_turn, self._ended_in_turn = self._ended_in_turn, False
        if self.stopping or self.interrupted or self._result_written():
            await self._close_input()
        elif self.background.pending:
            self._arm(self.background_wait, self._leave_background)
        elif ended_in_turn:
            self._arm(self.turn_grace, self._close_input)
        else:
            await self._close_input()  # the CLI ends once it has done what it was given

    def _result_written(self) -> bool:
        path = Path(self.context.worktree) / runs.RESULT_FILE
        return path.is_file() and not path.is_symlink()

    def _background_ended(self, tasks: list[str]) -> None:
        if self._turn_open:
            self._ended_in_turn = True
            return
        if not self._input_open:
            return
        self._unseen.extend(tasks)
        self._arm(self.follow_up_after, self._follow_up)

    async def _follow_up(self) -> None:
        """Background commands ended while no turn ran, and the CLI opened none for them: tell the agent."""
        ended = "\n".join(self.background.describe(task) for task in self._unseen)
        async with self._lock:
            if not self._input_open or self._client is None or self._turn_open:
                return
            self._turn_starts()
            await self._client.query(FOLLOW_UP.format(ended=ended))

    async def _leave_background(self) -> None:
        running = ", ".join(repr(description or task) for task, description in self.background.running.items())
        log.warning(
            "background commands still run after the agent's turn ended; ending the session",
            extra={"runtime": self.runtime, "waited_s": self.background_wait, "running": running or None},
        )
        await self._close_input()

    def _arm(self, seconds: float, action) -> None:
        """Run ``action`` in ``seconds``, instead of what was armed before."""
        self._disarm()
        self._timer = asyncio.create_task(self._after(seconds, action))

    def _disarm(self) -> None:
        if self._timer is not None and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None

    async def _after(self, seconds: float, action) -> None:
        await asyncio.sleep(seconds)
        self._timer = None
        try:
            await action()
        except Exception:  # the CLI is gone, or took no message: end the input, so the session ends
            log.warning("the agent's session did not go on", extra={"runtime": self.runtime}, exc_info=True)
            await self._close_input()

    async def _close_input(self) -> None:
        self._disarm()
        async with self._lock:
            if self._input_open:
                self._input_open = False
                self._end_input.set()

    async def send(self, text: str) -> None:
        async with self._lock:
            if not self._input_open or self._client is None:
                raise AgentFinished("claude-code has finished its turn and takes no more messages")
            if not self._turn_open:  # the message opens a turn: what waited for the CLI does no longer
                self._turn_starts()
            await self._client.query(text)

    async def _interrupt(self) -> None:
        if self._client is not None and self._input_open:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._client.interrupt(), INTERRUPT_TIMEOUT)
        await self._close_input()

    async def _stop_at_boundary(self) -> None:
        await self._close_input()

    async def _close(self) -> None:
        self._disarm()
        self._input_open = False
        self._end_input.set()
        if self._client is not None:
            await self._client.disconnect()

    def _cleanup(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
