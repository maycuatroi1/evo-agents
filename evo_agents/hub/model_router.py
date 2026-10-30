"""A run's model chosen by its difficulty (plan super-board-parity-2, criterion a6): the rules the hub and the worker
share. Standard library only: the worker imports it.

The project's policy has the section ``routing`` (``evo_agents.hub.autonomy``), off by default: ``enabled``, the
``scorer`` model, and the ``ladder`` from each difficulty to a model. While it is on, the claim of a run that names
no model carries the scorer and the ladder (``claim_routing``), and the worker, before the agent starts, has the run's
runtime rate the run's prompt with one short turn of the scorer (``evo_agents.worker.model_router``). The hub calls no
model at the dispatch.

- Only a run of ROUTED_KINDS on ROUTED_RUNTIMES is rated: the ladder names Claude Code's models. The Judge of the merge
  queue and of the Curator keeps the rule of ``evo_agents.hub.judges``, and the other runs of the Curator keep the
  model of the charter.
- A run pinned to a model is not rated: the dispatch named one (``--model``), or its agent profile allows exactly one
  (``pinned_by_profile``). A model the profile's allowlist leaves out is refused at the dispatch as before; a ladder
  model the allowlist leaves out is not used, and the session keeps the model it would have had.
- A rating the scorer did not give (it failed, timed out, or answered no difficulty) runs the run on the model of
  FALLBACK, the ladder's middle.

The worker records on the run what the routing did (``record``): the difficulty, the scorer, what the rating cost, the
model it chose, and why not when it chose none; and for every run, routed or not, the model its runtime used
(``runs.model_used``), so the cost of runs can be read by model.
"""

from __future__ import annotations

from collections.abc import Mapping

LEVELS = EASY, MEDIUM, HARD = ("easy", "medium", "hard")
FALLBACK = MEDIUM  # the difficulty of a run the scorer did not rate
ROUTED_KINDS = ("step", "plan", "author", "task")  # the kinds of run whose model the routing may choose
ROUTED_RUNTIMES = ("claude-code",)  # the runtimes whose models the ladder names
MAX_PROMPT_CHARS = 12_000  # of the run's prompt the scorer reads: its start, where the task is
MAX_ERROR_CHARS = 300
SCORER_PROMPT = """Rate how hard the coding task below is for an AI coding agent, as one word:
easy: a small, local change with an obvious way to do and check it;
medium: a change across a few files, or one that needs some reading of the code first;
hard: a design across modules, a migration, concurrency or security, or a task whose way is unclear.
Answer with the word alone: easy, medium or hard.

The task:

{prompt}"""


def claim_routing(policy: Mapping | None, kind: str, runtime: str, model: str | None) -> dict | None:
    """The routing the claim of a run carries, from the effective ``policy`` of its project: the scorer and the
    ladder; None when the policy has routing off, the run names a model, or its kind or runtime is not routed."""
    routing = (policy or {}).get("routing")
    if not isinstance(routing, Mapping) or routing.get("enabled") is not True:
        return None
    if (isinstance(model, str) and model.strip()) or kind not in ROUTED_KINDS or runtime not in ROUTED_RUNTIMES:
        return None
    ladder = routing.get("ladder") if isinstance(routing.get("ladder"), Mapping) else {}
    if not isinstance(routing.get("scorer"), str) or any(not isinstance(ladder.get(level), str) for level in LEVELS):
        return None
    return {"scorer": routing["scorer"], "ladder": {level: ladder[level] for level in LEVELS}}


def pinned_by_profile(allowed: list[str] | tuple[str, ...] | None) -> bool:
    """Whether an agent profile whose model allowlist is ``allowed`` (None: it limits none) pins the run's model."""
    return allowed is not None and len(allowed) == 1


def scorer_prompt(prompt: str) -> str:
    """What the scorer reads: the question, and the start of the run's prompt."""
    return SCORER_PROMPT.format(prompt=prompt[:MAX_PROMPT_CHARS])


def difficulty_of(answer: str | None) -> str | None:
    """The difficulty the scorer's ``answer`` names: the first word of it that is one of LEVELS; None for none."""
    if not isinstance(answer, str):
        return None
    for word in answer.lower().replace("*", " ").replace(".", " ").replace(":", " ").split():
        if word in LEVELS:
            return word
    return None


def record(
    scorer: str,
    difficulty: str | None,
    model: str | None,
    cost_usd: float | None = None,
    error: str | None = None,
) -> dict:
    """What the routing of a run did, as the run keeps it (``runs.routing``): ``difficulty`` is FALLBACK and
    ``scored`` false when the scorer gave none; ``model`` is None when the run did not take the ladder's."""
    return {
        "scorer": scorer,
        "difficulty": difficulty or FALLBACK,
        "scored": difficulty is not None,
        "model": model,
        "cost_usd": cost_usd,
        "error": error[:MAX_ERROR_CHARS] if error else None,
    }
