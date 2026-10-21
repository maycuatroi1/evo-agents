"""Who judges a change: the runtime and model of the Curator's Judge (``judge_runtime``) and of the Judges of the hub's
merge queue on GitLab (``merge_judge_model``). Pure functions, standard library only; ``evo_agents.hub.judge`` names
the Curator's two as it did before they moved here, and ``evo_agents.hub.server.merge_judge`` queues the queue's.

The Curator's Judge runs on Codex when the project's policy declares a sink for Codex (an id ``codex@...``) whose
clearance covers the project's label and the worker on duty has it, else on Claude Code with a model other than the
Builder's. The merge queue's Judge always runs on Claude Code, never Codex (the member's choice of 2026-10-20: a Judge
of the same family as the Builder, made up for by the deterministic gates, the hidden checks, the second Judge and the
audit samples): the first with the policy's ``judge.model`` when it is not the Builder's model, else a Claude model
other than the Builder's; a person's merge request, which no Builder made, with ``judge.model`` or the runtime's
default; the second Judge, of a merge request of high risk, with Opus.
"""

from __future__ import annotations

CODEX_SINK = "codex@"  # the start of the id of a sink of the Codex runtime in a project's policy
CLAUDE_CODE = "claude-code"
SECOND_JUDGE_MODEL = "opus"  # the Judge of a merge request of high risk


def codex_cleared(rules, label: dict | None) -> bool:
    """Whether the project's policy (``rules``, an ``access.ProjectRules``) declares a sink for Codex whose clearance
    covers ``label``, the project's label as stored: an id starting with CODEX_SINK, of a level and location the
    project's ladder knows, at or above the label's."""
    stored = rules.stored(label) if label is not None else rules.top()
    if stored is None or not stored.projects <= {rules.name}:
        return False
    closed = getattr(rules, "_closed", set())
    for sink_id, clearance in rules.policy.sinks.items():
        if not str(sink_id).lower().startswith(CODEX_SINK) or sink_id in closed:
            continue
        if stored.level <= clearance.level and stored.location <= clearance.location:
            return True
    return False


def other_model(model: str | None) -> str:
    """A Claude model other than ``model`` (the Builder's; None is the runtime's default, Opus)."""
    return "sonnet" if model is None or "opus" in model.lower() else "opus"


def judge_runtime(
    judge: dict, builder: dict, *, codex_allowed: bool, worker_runtimes: set[str] | frozenset[str]
) -> tuple[str, str | None, str]:
    """(runtime, model, why) of the Curator's Judge, from the charter's ``judge`` and ``builder`` roles: Codex when
    the policy clears it and the worker on duty has it, else Claude Code with a model other than the Builder's."""
    wanted_model = judge.get("model")
    if codex_allowed and "codex" in worker_runtimes:
        model = wanted_model if judge.get("runtime") == "codex" else None
        return "codex", model, "the project's policy clears Codex for its label"
    why = (
        "the project's policy declares no sink for Codex that clears its label"
        if not codex_allowed
        else "the worker on duty has no Codex"
    )
    builder_model = builder.get("model")
    if builder.get("runtime", CLAUDE_CODE) == CLAUDE_CODE:
        chosen = wanted_model if judge.get("runtime") == CLAUDE_CODE else None
        if chosen is None or chosen == builder_model:
            chosen = other_model(builder_model)
        return CLAUDE_CODE, chosen, f"{why}: Claude Code with a model other than the Builder's"
    chosen = wanted_model if judge.get("runtime") == CLAUDE_CODE else None
    return CLAUDE_CODE, chosen, f"{why}: Claude Code, another runtime than the Builder's"


def merge_judge_model(
    policy_model: str | None, builder_model: str | None, *, built: bool, second: bool = False
) -> tuple[str | None, str]:
    """(model, why) of a Judge of the merge queue, which runs on Claude Code: SECOND_JUDGE_MODEL for the second Judge;
    else the policy's ``judge.model`` unless it is the Builder's; else, for a merge request a run ``built``, a Claude
    model other than the Builder's (``builder_model``, None for the runtime's default), and for a person's, None (the
    runtime's default)."""
    if second:
        return SECOND_JUDGE_MODEL, "the second Judge of a merge request of high risk runs Opus"
    if policy_model and (not built or policy_model != builder_model):
        return policy_model, "the policy's judge.model"
    if not built:
        return None, "a person's merge request: the runtime's default model"
    return other_model(builder_model), "a Claude model other than the Builder's"
