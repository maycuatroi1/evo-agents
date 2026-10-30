"""The rating of a run's difficulty before its agent starts, and the model it gives the run (plan
super-board-parity-2, criterion a6; the shared rules are ``evo_agents.hub.model_router``).

A claim carries ``routing`` (the scorer and the ladder) only while the project's policy has routing on, for a run that
names no model. ``route`` then:

- does nothing for a run pinned to a model: the run names one, or its agent profile allows exactly one;
- has the run's runtime rate the run's prompt with one short turn of the scorer: ``claude -p`` on the scorer's model,
  no tool, no MCP server, no session kept, in an empty directory, with the agent's environment so it uses the run's
  account, cut off after SCORER_TIMEOUT seconds;
- takes the ladder's model for the difficulty the scorer named, or for FALLBACK (the middle) when the scorer failed or
  named none; a model the run's agent profile does not allow is not taken, and the session keeps its model;
- returns what it did as the run keeps it (``model_router.record``), which the worker reports with the run's state.

Nothing here fails the run: a scorer that cannot run is a rating not given.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from collections.abc import Mapping

from evo_agents.hub import model_router
from evo_agents.worker.runtimes import profiles
from evo_agents.worker.runtimes.common import which

SCORER_TIMEOUT = 120.0  # seconds the scorer's turn may take
CLAUDE = "claude"
MAX_OUTPUT_BYTES = 1024 * 1024


def routing_of(spec: Mapping) -> dict | None:
    """The routing of the run's claim (``spec``) when its model is to be chosen here: None for a claim without one, and
    for a run pinned to a model by the dispatch or by its agent profile."""
    routing = spec.get("routing")
    if not isinstance(routing, Mapping) or not isinstance(routing.get("ladder"), Mapping):
        return None
    model = spec.get("model")
    if isinstance(model, str) and model.strip():
        return None
    profile = profiles.Profile.of(spec)
    if profile is not None and model_router.pinned_by_profile(profile.allowed("model")):
        return None
    return dict(routing)


def scorer_argv(binary: str, scorer: str) -> list[str]:
    """The command of the scorer's one turn on Claude Code; the question goes on stdin."""
    return [
        binary,
        "--print",
        "--model",
        scorer,
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--tools",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]


def read_answer(returncode: int | None, stdout: bytes, stderr: bytes) -> tuple[str | None, float | None, str | None]:
    """(answer, cost in USD, error) of the scorer's turn from what ``claude --print --output-format json`` wrote."""
    try:
        data = json.loads(stdout.decode(errors="replace") or "null")
    except ValueError:
        data = None
    cost = data.get("total_cost_usd") if isinstance(data, dict) else None
    cost = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0 else None
    if not isinstance(data, dict):
        tail = (stderr or stdout).decode(errors="replace").strip()[-200:]
        return None, cost, f"the scorer exited {returncode} without its JSON answer: {tail or 'no output'}"
    answer = data.get("result") if isinstance(data.get("result"), str) else None
    if returncode != 0 or data.get("is_error") is True:
        return None, cost, f"the scorer failed (exit {returncode}): {(answer or data.get('subtype') or '')[:200]}"
    return answer, cost, None


async def score(scorer: str, prompt: str, env: Mapping[str, str], timeout: float = SCORER_TIMEOUT):
    """(answer, cost in USD, error) of one turn of ``scorer`` on Claude Code rating ``prompt``."""
    try:
        binary = which(CLAUDE, env)
    except RuntimeError as exc:
        return None, None, str(exc)
    question = model_router.scorer_prompt(prompt).encode()
    with tempfile.TemporaryDirectory(prefix="evo-scorer-") as cwd:
        try:
            process = await asyncio.create_subprocess_exec(
                *scorer_argv(binary, scorer),
                cwd=cwd,
                env=dict(env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return None, None, f"the scorer did not start: {exc}"
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(question), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return None, None, f"the scorer gave no answer within {timeout:g}s"
    return read_answer(process.returncode, stdout[:MAX_OUTPUT_BYTES], stderr[-MAX_OUTPUT_BYTES:])


async def route(spec: Mapping, env: Mapping[str, str], timeout: float = SCORER_TIMEOUT) -> dict | None:
    """What the routing of the run of ``spec`` did (``model_router.record``), the model it chose in ``model``; None for
    a run whose model is not chosen here (``routing_of``)."""
    routing = routing_of(spec)
    if routing is None:
        return None
    scorer, ladder = str(routing.get("scorer") or ""), routing["ladder"]
    answer, cost, error = await score(scorer, str(spec.get("prompt") or ""), env, timeout)
    difficulty = model_router.difficulty_of(answer)
    if difficulty is None and error is None:
        error = f"the scorer named no difficulty: {(answer or '').strip()[:100]!r}"
    model = ladder.get(difficulty or model_router.FALLBACK)
    profile = profiles.Profile.of(spec)
    allowed = profile.allowed("model") if profile is not None else None
    if allowed is not None and model not in allowed:
        refused = f"agent profile {profile.label} does not allow the ladder's model {model}"
        error, model = (f"{error}; {refused}" if error else refused), None
    return model_router.record(scorer, difficulty, model if isinstance(model, str) else None, cost, error)
