"""A run's model chosen by its difficulty and the project's Token strategy (plans super-board-parity-2, criterion a6,
and token-and-quota-economy, criterion a3): the rules the hub and the worker share. Standard library only: the worker
imports it.

The project's policy has the section ``routing`` (``evo_agents.hub.autonomy``), off by default: ``enabled``, the
``strategy``, ``hard_reserve_pct``, ``five_hour_ceiling_pct``, the ``scorer`` model (``off``: none), the ``ladder``
from each difficulty to a model and its ``overflow``, a model of each difficulty for when the subscription runs low.
A model is a runtime's own name (``haiku``, ``sonnet``, ``opus``) or a gateway model, ``<prefix>/<model>``.

While routing is on, the hub rates a run that names no model before its credentials are leased, with no model call
(``rate``):

- a step that says its ``difficulty`` has it;
- a step about a migration or security, the plan's last step when it merges or deploys, and a prescribed step (one
  without ``acceptance``) are hard; a step whose files are documents alone, or that exports or documents, is easy; any
  other step is medium;
- a plan run is hard, an author run medium unless its request reads hard; a short question is easy;
- a chat run that works on plan steps is at least as hard as the hardest of them (``with_steps``): the coordinator's
  session of a plan handed to a system covers its open steps, an ask whose brief names a step that step;
- a free prompt (a task, a chat turn, a long question) is rated by the words it holds, then by the role of its agent;
  one that says nothing either way is left to the scorer, one short turn on the worker when ``scorer`` is not ``off``,
  and is medium (FALLBACK) when it is.

A run of the same step, or a rerun of a run, whose run before it failed on easy or medium is rated one level higher
(``escalate``). ``choose`` then takes the ladder's model or the overflow's for that difficulty by the strategy and the
quota of the subscription (``Quota``: the most of a weekly window left among the run's own accounts, a window not
reported or past its reset counting unused):

- ``surplus-first`` (the default) runs on the ladder while the subscription has more than ``hard_reserve_pct`` of its
  week left and its five-hour window is under ``five_hour_ceiling_pct``; past that an easy or medium run takes the
  overflow, and a hard one keeps the ladder until the subscription is used up;
- ``cost-first`` always gives an easy or medium run the overflow's model, the cheap one, and a hard run the ladder's;
- ``quota-burn`` gives every run the ladder's hard model while the subscription is out of its reserve and under its
  ceiling, and goes as surplus-first past that.

An overflow difficulty without a model keeps the ladder's. The hub keeps what it chose on the run (``runs.routing``,
``record``), with the difficulty, where it came from (``source``), why, the strategy, the branch and the level it
escalated from, and hands the worker the routing of the claim; a gateway model leases the run a gateway key, a
runtime's own model the native accounts. The worker records the model its runtime used (``runs.model_used``).

- Only a run of ROUTED_KINDS on ROUTED_RUNTIMES is rated. The Judge of the merge queue and of the Curator keeps the rule
  of ``evo_agents.hub.judges``, and the other runs of the Curator keep the model of the charter.
- A run pinned to a model is not rated: the dispatch named one (``--model``), or its agent profile allows exactly one
  (``pinned_by_profile``). A model the profile's allowlist leaves out is refused at the dispatch as before; a chosen
  model the allowlist leaves out is not used, and the session keeps the model it would have had.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

LEVELS = EASY, MEDIUM, HARD = ("easy", "medium", "hard")
FALLBACK = MEDIUM  # the difficulty of a run neither the rules nor the scorer rated
ROUTED_KINDS = ("step", "plan", "author", "task", "chat", "question")  # the kinds of run whose model may be chosen
ROUTED_RUNTIMES = ("claude-code", "opencode", "codex", "any")
NATIVE_RUNTIME = "claude-code"  # the runtime whose own models the ladder names without a prefix
STRATEGIES = SURPLUS_FIRST, COST_FIRST, QUOTA_BURN = ("surplus-first", "cost-first", "quota-burn")
DEFAULT_STRATEGY = SURPLUS_FIRST
SCORER_OFF = "off"  # routing.scorer: no scorer; a free prompt the rules cannot rate is FALLBACK
SOURCES = ("plan", "rule", "role", "scorer", "fallback")  # where a run's difficulty came from
BRANCHES = LADDER, OVERFLOW = ("ladder", "overflow")
# the keys the hub's rating adds to a run's record (``record``), left out while null
HUB_KEYS = ("source", "reason", "strategy", "branch", "escalated_from", "gateway", "quota", "pending", "choices")
MAX_PROMPT_CHARS = 12_000  # of the run's prompt the scorer reads: its start, where the task is
MAX_ERROR_CHARS = 300
MAX_REASON_CHARS = 300
SHORT_QUESTION_CHARS = 600  # a question this long or shorter is easy
SHORT_PROMPT_CHARS = 200  # a task or chat turn this short, with words of easy work, is easy
SCORER_PROMPT = """Rate how hard the coding task below is for an AI coding agent, as one word:
easy: a small, local change with an obvious way to do and check it;
medium: a change across a few files, or one that needs some reading of the code first;
hard: a design across modules, a migration, concurrency or security, or a task whose way is unclear.
Answer with the word alone: easy, medium or hard.

The task:

{prompt}"""

HARD_WORDS = re.compile(
    r"\b(migrat\w*|security|secure|vulnerab\w*|cve-\d+|concurren\w*|race condition|deadlock|encrypt\w*|"
    r"architect\w*|redesign)\b|bảo mật|lỗ hổng|kiến trúc",
    re.IGNORECASE,
)
EASY_WORDS = re.compile(
    r"\b(docs?|documentation|readme|changelog|typo|markdown|export\w*|translat\w*|comment|wording|spelling)\b|"
    r"tài liệu|chính tả|dịch",
    re.IGNORECASE,
)
SHIP_WORDS = re.compile(r"\b(merge|deploy|release)\b", re.IGNORECASE)
DOC_FILE = re.compile(r"[\w./-]+\.(md|mdx|rst|txt|adoc)\b", re.IGNORECASE)
CODE_FILE = re.compile(r"[\w./-]+\.(py|pyi|ts|tsx|js|jsx|mjs|go|rs|java|kt|rb|php|cs|c|h|cpp|sql|sh|vue|svelte)\b")
EASY_ROLES = re.compile(r"doc|writer|support|translat|triage", re.IGNORECASE)
HARD_ROLES = re.compile(r"architect|security|lead|principal", re.IGNORECASE)


@dataclass(frozen=True)
class Rating:
    """A run's difficulty and where it came from; ``difficulty`` None: the rules could not tell (the scorer may)."""

    difficulty: str | None
    source: str
    reason: str


@dataclass(frozen=True)
class Quota:
    """What the subscription of a run has left: the share of its weekly window left, 0 to 1, and the share of its
    five-hour window used. ``known`` False: no account of it reported any, so it counts unused."""

    weekly_left: float = 1.0
    five_hour_used: float = 0.0
    known: bool = False

    def view(self) -> dict | None:
        if not self.known:
            return None
        return {"weekly_left": round(self.weekly_left, 4), "five_hour_used": round(self.five_hour_used, 4)}


@dataclass(frozen=True)
class Choice:
    model: str
    branch: str
    reason: str


def claim_routing(policy: Mapping | None, kind: str, runtime: str, model: str | None) -> dict | None:
    """The routing a claim carries for a run of the project whose effective policy is ``policy``: the scorer and the
    ladder; None when the policy has routing off, the run names a model, or its kind or runtime is not routed."""
    routing = routing_section(policy)
    if routing is None:
        return None
    if (isinstance(model, str) and model.strip()) or kind not in ROUTED_KINDS or runtime not in ROUTED_RUNTIMES:
        return None
    return {"scorer": routing["scorer"], "ladder": {level: routing["ladder"][level] for level in LEVELS}}


def routing_section(policy: Mapping | None) -> dict | None:
    """The section ``routing`` of an effective ``policy`` while it is on and reads; None otherwise."""
    routing = (policy or {}).get("routing")
    if not isinstance(routing, Mapping) or routing.get("enabled") is not True:
        return None
    ladder = routing.get("ladder") if isinstance(routing.get("ladder"), Mapping) else {}
    if not isinstance(routing.get("scorer"), str) or any(not isinstance(ladder.get(level), str) for level in LEVELS):
        return None
    return dict(routing)


def pinned_by_profile(allowed: list[str] | tuple[str, ...] | None) -> bool:
    """Whether an agent profile whose model allowlist is ``allowed`` (None: it limits none) pins the run's model."""
    return allowed is not None and len(allowed) == 1


def is_gateway_model(model: str | None) -> bool:
    """Whether ``model`` reads ``<prefix>/<model>``, as a gateway model does (the hub checks the prefix)."""
    return isinstance(model, str) and "/" in model.strip("/")


def routed_model(routing) -> str:
    """The model the hub's routing of a run (``runs.routing``) chose; empty when it chose none."""
    model = routing.get("model") if isinstance(routing, Mapping) else None
    return model if isinstance(model, str) else ""


# Rating


def _text(*parts) -> str:
    return "\n".join(str(part) for part in parts if part)


def _docs_only(text: str) -> bool:
    """Whether ``text`` names files and they are documents alone, or names none and speaks of easy work alone."""
    docs, code = DOC_FILE.search(text) is not None, CODE_FILE.search(text) is not None
    return (docs and not code) or (not code and EASY_WORDS.search(text) is not None and not docs)


def _last(plan: Mapping | None, step: Mapping) -> bool:
    steps = plan.get("steps") if isinstance(plan, Mapping) else None
    return isinstance(steps, list) and bool(steps) and steps[-1] is step


def rate_step(step: Mapping, plan: Mapping | None = None) -> Rating:
    """The difficulty of a plan's step, as the module says."""
    said = step.get("difficulty")
    if said in LEVELS:
        return Rating(said, "plan", f"the step says difficulty {said}")
    text = _text(step.get("title"), step.get("what"), step.get("where"), step.get("note"))
    if HARD_WORDS.search(_text(text, step.get("verify"))):
        return Rating(HARD, "rule", "the step is about a migration, security or a design across modules")
    if _last(plan, step) and SHIP_WORDS.search(text):
        return Rating(HARD, "rule", "the plan's last step: the tests of the whole diff, then merge and deploy")
    if _docs_only(text):  # not its verify: the files a check reads are not the files the step changes
        return Rating(EASY, "rule", "the step changes documents alone, or exports")
    if not step.get("acceptance"):
        return Rating(HARD, "rule", "a prescribed step, without acceptance: its operations exactly as written")
    return Rating(MEDIUM, "rule", "an outcome step")


def rate_prompt(kind: str, prompt: str, role: str | None = None) -> Rating:
    """The difficulty of a run of ``kind`` whose prompt (or request) is ``prompt``, as the module says;
    ``difficulty`` None when nothing in it, nor the role of its agent, says."""
    prompt = prompt or ""
    if kind == "plan":
        return Rating(HARD, "rule", "a plan run takes every step to its end, the last one's merge included")
    if HARD_WORDS.search(prompt):
        return Rating(HARD, "rule", "the prompt speaks of a migration, security or a design across modules")
    if kind == "author":
        return Rating(MEDIUM, "rule", "an author run writes a plan")
    if kind == "question" and len(prompt.strip()) <= SHORT_QUESTION_CHARS:
        return Rating(EASY, "rule", "a short question")
    if EASY_WORDS.search(prompt) and CODE_FILE.search(prompt) is None:
        return Rating(EASY, "rule", "the prompt speaks of documents, wording or an export alone")
    if role and HARD_ROLES.search(role):
        return Rating(HARD, "role", f"the run's agent {role} does hard work")
    if role and EASY_ROLES.search(role):
        return Rating(EASY, "role", f"the run's agent {role} does easy work")
    return Rating(None, "scorer", "the prompt says nothing of its difficulty")


def with_steps(rating: Rating, steps: list[tuple[str, Rating]]) -> Rating:
    """``rating`` of a chat run's prompt, at least as hard as the hardest of ``steps``, the plan steps it works on
    (key, ``rate_step``): a plan handed to a system, or an ask that names a step, is rated by its steps, not its
    words, which speak of documents and exports whatever the steps change."""
    rated = [(key, step) for key, step in steps if step.difficulty in LEVELS]
    if not rated:
        return rating
    key, hardest = max(rated, key=lambda item: LEVELS.index(item[1].difficulty))
    if rating.difficulty in LEVELS and LEVELS.index(rating.difficulty) >= LEVELS.index(hardest.difficulty):
        return rating
    return Rating(hardest.difficulty, "plan", f"the hardest plan step it works on, step {key}: {hardest.reason}")


def escalate(level: str | None) -> str:
    """One level up from ``level``: easy to medium, medium to hard; hard stays."""
    if level not in LEVELS:
        return FALLBACK
    return LEVELS[min(LEVELS.index(level) + 1, len(LEVELS) - 1)]


def at_least(level: str, floor: str | None) -> str:
    """The harder of ``level`` and ``floor``."""
    if floor not in LEVELS:
        return level
    return LEVELS[max(LEVELS.index(level), LEVELS.index(floor))]


# Choosing


def _models(routing: Mapping, name: str) -> dict:
    found = routing.get(name)
    return dict(found) if isinstance(found, Mapping) else {}


def _pct(routing: Mapping, name: str, default: int) -> float:
    value = routing.get(name)
    if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 100:
        return value / 100
    return default / 100


def choose(routing: Mapping, difficulty: str, quota: Quota | None = None) -> Choice:
    """The model of a run of ``difficulty`` under the Token strategy ``routing`` (the policy's section) and the
    subscription's ``quota``, as the module says."""
    quota = quota or Quota()
    ladder, overflow = _models(routing, "ladder"), _models(routing, "overflow")
    strategy = routing.get("strategy") if routing.get("strategy") in STRATEGIES else DEFAULT_STRATEGY
    reserve, ceiling = _pct(routing, "hard_reserve_pct", 10), _pct(routing, "five_hour_ceiling_pct", 90)
    spare = quota.weekly_left > reserve and quota.five_hour_used < ceiling
    spent = quota.weekly_left <= 0 or quota.five_hour_used >= 1

    def cheap(why: str) -> Choice:
        if isinstance(overflow.get(difficulty), str):
            return Choice(overflow[difficulty], OVERFLOW, why)
        return Choice(ladder[difficulty], LADDER, f"{why}; the overflow names no {difficulty} model")

    if strategy == COST_FIRST:
        if difficulty != HARD:
            return cheap(f"cost-first: a {difficulty} run takes the cheap model")
        if not spent:
            return Choice(ladder[HARD], LADDER, "cost-first: a hard run takes the ladder's model")
        return cheap("cost-first: the subscription is used up")
    if strategy == QUOTA_BURN and spare:
        return Choice(ladder[HARD], LADDER, "quota-burn: the subscription is out of its reserve, so the strongest")
    if spare:
        return Choice(ladder[difficulty], LADDER, f"{strategy}: the subscription has its week to spend")
    if difficulty == HARD and not spent:
        return Choice(ladder[HARD], LADDER, f"{strategy}: the subscription's reserve is kept for hard work")
    why = "used up" if spent else "in its reserve or hot in its five hours"
    return cheap(f"{strategy}: the subscription is {why}")


# The scorer, on the worker


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
    **decided,
) -> dict:
    """What the routing of a run did, as the run keeps it (``runs.routing``): ``difficulty`` is FALLBACK and
    ``scored`` false when neither the hub nor the scorer gave one; ``model`` is None when the run did not take the
    chosen model. ``decided`` holds what the hub's rating adds: source, reason, strategy, branch, escalated_from,
    quota, pending and choices; a key of it that is None is left out."""
    found = {
        "scorer": scorer,
        "difficulty": difficulty or FALLBACK,
        "scored": difficulty is not None and decided.get("source", "scorer") == "scorer",
        "model": model,
        "cost_usd": cost_usd,
        "error": error[:MAX_ERROR_CHARS] if error else None,
    }
    if isinstance(decided.get("reason"), str):
        decided["reason"] = decided["reason"][:MAX_REASON_CHARS]
    found.update({key: value for key, value in decided.items() if value is not None})
    return found
