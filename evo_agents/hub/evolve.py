"""The Curator's evolution of a harness as the hub models it: a population of candidates kept on the Pareto front of the
tasks they were measured on, the parents of the next candidate drawn by how often each is on that front, and the merge
of two candidates through their nearest common ancestor. Pure functions over JSON values, standard library only, so the
api, the hub's worker, the worker daemon and the tests share them; ``docs/curator.md`` has the design, and the proofs
of the operators are the Lean 4 notes of the harness (``research_notes/gepa-pareto/GepaPareto.lean``), after GEPA's
``remove_dominated_programs`` and ``MergeProposer`` (Agrawal et al. 2025).

A candidate is a set of components: what each path of the harness it rewrites holds (a ``SKILL.md`` and the other
files of its skill, an ``AGENTS.md``, a ``CLAUDE.md``) or, for an agent of an agent system, its ``instructions`` and
its ``skill`` (AGENT_AXES). A component's value is the whole text, None for a file the candidate deletes. Nothing here
caps the length of a component: the proposer rewrites a file whole, and the measure, not a budget of characters, says
whether the rewrite is better.

The operators, each as the Lean notes state and prove it:

- ``ceiling``: the best score any candidate of the pool reached on each task (a score missing counts 0);
- ``is_redundant``: a candidate is redundant among the survivors when every task on which it reaches the ceiling has
  another survivor that reaches it too;
- ``prune_all``: scans the pool, the weakest first (the lowest total score, then the highest cost, then the most turns,
  then the newest), and drops each candidate redundant among those left. Every task keeps a survivor at its ceiling,
  and a candidate that alone reaches the ceiling of one task (a unique specialist) is never dropped; between two equal
  candidates the cheaper one is kept;
- ``front_freq``: on how many tasks each survivor reaches the ceiling, the weight ``pick_parent`` draws a parent by;
- ``lca_merge``: the three-way merge of two candidates over their nearest common ancestor (``lca``), component by
  component: what one side changed and the other kept is taken; what both changed alike is taken once; what both
  changed apart is a conflict, which an agent merges (``synthesis_request``, then ``apply_synthesis``).

The tasks of an evolution are light ones of the ``select`` split of the project's replay tasks
(``evo_agents.hub.replay``) and the project's active probes of the paths it evolves (``light_tasks``): a task of the
``holdout`` split is never read, since the replays of the change the evolution ends with are measured on them. The
commits of a task recorded before the repo's history was rewritten (``git filter-repo``) are read through the
commit-map that rewrite wrote (``parse_commit_map``). The baseline arm of a task runs at most once for one baseline:
``arm_key`` names its result by task kind, task, base commit, digest of the baseline's components, runtime and model.

Each candidate is kept with its actionable side information (ASI, GEPA's diagnostic feedback), one entry per task it
was measured on: each failed check's exit code and the end of its output, the agent's failed tool calls, error and last
message, never a verify command. ``asi_of`` bounds each entry (ASI_TEXT characters a text, ASI_ITEMS items a list); the
candidate itself, its components, is never cut.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from evo_agents.hub import replay, tiers

Target = Literal["git", "agent_config"]  # the files of a harness in a repo, or an agent of an agent system
TARGETS: tuple[Target, ...] = get_args(Target)
AGENT_AXES = ("instructions", "skill")  # the components of an agent_config candidate
Origin = Literal["seed", "mutate", "merge"]  # how a candidate came to be
ORIGINS: tuple[Origin, ...] = get_args(Origin)
TaskKind = Literal["replay", "probe"]  # a task of the replay suite's select split, or an active probe
TASK_KINDS: tuple[TaskKind, ...] = get_args(TaskKind)
SELECT = replay.SPLITS[0]
DEFAULT_TASKS = 8  # light replay tasks one evolution measures on, by default
DEFAULT_PROBES = 4  # probes, by default
DEFAULT_MAX_FILES = 8  # a light task's commits change at most this many files
DEFAULT_MAX_MS = 20 * 60 * 1000  # and its step ran at most this long, when that is known
EVOLVE_KEY_HEADER = "X-Evo-Evolve-Key"  # an evolve run's own credential, which its claim hands the daemon alone
GONE_SHA = re.compile(r"^0+$")  # what a commit-map writes for a commit the rewrite dropped
FULL_SHA = replay.SHA
MIN_PREFIX = 7  # a short sha the commit-map is read by
ASI_TEXT = 4000  # characters of one text of a task's ASI: its end, where a failure says why
ASI_ITEMS = 20  # items of one list of a task's ASI
ASI_DEPTH = 4  # levels of nesting of a task's ASI


class EvolveProblem(ValueError):
    """What an evolution cannot do with its input; ``str`` says why."""


# The Pareto front


def _score(scores: Mapping[str, Mapping[str, float]], candidate: str, task: str) -> float:
    value = (scores.get(candidate) or {}).get(task)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def tasks_of(scores: Mapping[str, Mapping[str, float]]) -> list[str]:
    """Every task any candidate of ``scores`` (candidate -> task -> score) was measured on, sorted."""
    return sorted({task for row in scores.values() for task in (row or {})})


def ceiling(scores: Mapping[str, Mapping[str, float]], tasks: Iterable[str] | None = None) -> dict[str, float]:
    """task -> the best score of any candidate of ``scores`` on it, a missing score 0."""
    tasks = tasks_of(scores) if tasks is None else list(tasks)
    return {task: max((_score(scores, cand, task) for cand in scores), default=0.0) for task in tasks}


def fronts(scores: Mapping[str, Mapping[str, float]], tasks: Iterable[str] | None = None) -> dict[str, set[str]]:
    """task -> the candidates of ``scores`` that reach its ceiling."""
    top = ceiling(scores, tasks)
    return {task: {cand for cand in scores if _score(scores, cand, task) == best} for task, best in top.items()}


def is_redundant(
    scores: Mapping[str, Mapping[str, float]], survivors: Iterable[str], candidate: str, top: Mapping[str, float]
) -> bool:
    """Whether every task on which ``candidate`` reaches the ceiling ``top`` (task -> best of the whole pool) has
    another of ``survivors`` that reaches it too."""
    others = [cand for cand in survivors if cand != candidate]
    for task, best in top.items():
        if _score(scores, candidate, task) == best and not any(_score(scores, q, task) == best for q in others):
            return False
    return True


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def scan_order(
    scores: Mapping[str, Mapping[str, float]],
    costs: Mapping[str, Mapping[str, float]] | None = None,
    tasks: Iterable[str] | None = None,
) -> list[str]:
    """The candidates of ``scores`` the weakest first, as ``prune_all`` scans them: the lowest total score, then the
    highest cost (``costs``: candidate -> {cost_usd, turns}, a missing figure the highest), the most turns, the
    candidate named last in ``scores``."""
    tasks = tasks_of(scores) if tasks is None else list(tasks)
    position = {cand: index for index, cand in enumerate(scores)}

    def key(cand: str):
        found = (costs or {}).get(cand) or {}
        cost, turns = _number(found.get("cost_usd")), _number(found.get("turns"))
        total = sum(_score(scores, cand, task) for task in tasks)
        inf = float("inf")
        return (total, -(inf if cost is None else cost), -(inf if turns is None else turns), -position[cand])

    return sorted(scores, key=key)


def prune_all(
    scores: Mapping[str, Mapping[str, float]],
    costs: Mapping[str, Mapping[str, float]] | None = None,
    tasks: Iterable[str] | None = None,
) -> list[str]:
    """The survivors of the pool ``scores`` (candidate -> task -> score), in the pool's order: each candidate, the
    weakest first (``scan_order``), is dropped when it is redundant among those left (``is_redundant``). Every task
    keeps a survivor at its ceiling, and a unique specialist survives."""
    tasks = tasks_of(scores) if tasks is None else list(tasks)
    top = ceiling(scores, tasks)
    survivors = list(scores)
    for cand in scan_order(scores, costs, tasks):
        if is_redundant(scores, survivors, cand, top):
            survivors.remove(cand)
    return survivors


def front_freq(
    scores: Mapping[str, Mapping[str, float]],
    survivors: Iterable[str] | None = None,
    tasks: Iterable[str] | None = None,
) -> dict[str, int]:
    """survivor -> on how many tasks it reaches the ceiling of the whole pool ``scores``."""
    tasks = tasks_of(scores) if tasks is None else list(tasks)
    top = ceiling(scores, tasks)
    pool = list(scores) if survivors is None else list(survivors)
    return {cand: sum(_score(scores, cand, task) == best for task, best in top.items()) for cand in pool}


def pick_parent(freq: Mapping[str, int], rng: random.Random | None = None) -> str:
    """A parent drawn from ``freq`` (candidate -> weight, ``front_freq``) with a chance in proportion to its weight;
    the first when every weight is 0. EvolveProblem for an empty ``freq``."""
    if not freq:
        raise EvolveProblem("there is no candidate to draw a parent from")
    names = list(freq)
    weights = [max(int(freq[name]), 0) for name in names]
    if not any(weights):
        return names[0]
    return (rng or random.Random()).choices(names, weights=weights, k=1)[0]


def best(
    scores: Mapping[str, Mapping[str, float]],
    costs: Mapping[str, Mapping[str, float]] | None = None,
    tasks: Iterable[str] | None = None,
) -> str | None:
    """The candidate with the highest total score, the cheapest among equals, the earliest after that; None for an
    empty pool."""
    order = scan_order(scores, costs, tasks)
    return order[-1] if order else None


# The merge of two candidates


def ancestry(parents: Mapping[str, Sequence[str]], candidate: str) -> dict[str, int]:
    """Each ancestor of ``candidate`` (itself included, at 0) -> its fewest generations from it, through ``parents``
    (candidate -> its parents)."""
    found = {candidate: 0}
    queue = [candidate]
    while queue:
        current = queue.pop(0)
        for parent in parents.get(current) or ():
            if parent not in found:
                found[parent] = found[current] + 1
                queue.append(parent)
    return found


def lca(parents: Mapping[str, Sequence[str]], a: str, b: str) -> str | None:
    """The nearest common ancestor of ``a`` and ``b``: the one with the fewest generations from the two together, the
    lowest id among equals; None when they have none."""
    left, right = ancestry(parents, a), ancestry(parents, b)
    common = set(left) & set(right)
    if not common:
        return None
    return min(common, key=lambda cand: (left[cand] + right[cand], str(cand)))


@dataclass(frozen=True)
class Conflict:
    """A component both sides of a merge changed, apart: the agent of the merge writes it."""

    key: str
    ancestor: str | None
    ours: str | None
    theirs: str | None


@dataclass(frozen=True)
class Merged:
    """What ``lca_merge`` gave: the components it could take (``components``), which side each came from, and the
    conflicts left."""

    components: dict[str, str | None]
    from_ours: list[str] = field(default_factory=list)
    from_theirs: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def useful(self) -> bool:
        """Whether the merge is a new candidate: each side brings what the other lacks, or there is a conflict."""
        return bool(self.conflicts) or bool(self.from_ours and self.from_theirs)


def lca_merge(
    ancestor: Mapping[str, str | None], ours: Mapping[str, str | None], theirs: Mapping[str, str | None]
) -> Merged:
    """The three-way merge of ``ours`` and ``theirs`` over ``ancestor`` (each component -> its whole text, None or
    missing for no file), component by component: a side that kept the ancestor's takes the other's; equal sides take
    it once; sides that changed apart are a Conflict. A component None in the merge is dropped from it."""
    merged: dict[str, str | None] = {}
    from_ours, from_theirs, conflicts = [], [], []
    for key in sorted(set(ancestor) | set(ours) | set(theirs)):
        base, left, right = ancestor.get(key), ours.get(key), theirs.get(key)
        if left == right:
            value = left
        elif left == base:
            value = right
            from_theirs.append(key)
        elif right == base:
            value = left
            from_ours.append(key)
        else:
            conflicts.append(Conflict(key, base, left, right))
            continue
        if value is not None:
            merged[key] = value
    return Merged(merged, from_ours, from_theirs, conflicts)


def synthesis_request(merged: Merged, *, target: str = TARGETS[0]) -> dict:
    """What the agent that merges the conflicts of ``merged`` is handed: the target, the components taken, and each
    conflict whole (no text clipped)."""
    return {
        "target": target,
        "taken": sorted(merged.components),
        "conflicts": [
            {"key": item.key, "ancestor": item.ancestor, "ours": item.ours, "theirs": item.theirs}
            for item in merged.conflicts
        ],
    }


def apply_synthesis(merged: Merged, answers: Mapping[str, str | None]) -> dict[str, str | None]:
    """The components of ``merged`` with the agent's ``answers`` (conflict key -> its text, None to drop it);
    EvolveProblem when an answer is missing or names no conflict."""
    wanted = {item.key for item in merged.conflicts}
    missing, extra = wanted - set(answers), set(answers) - wanted
    if missing or extra:
        named = [f"no answer for {', '.join(sorted(missing))}"] if missing else []
        named += [f"{', '.join(sorted(extra))} is no conflict"] if extra else []
        raise EvolveProblem("the merge's answers: " + "; ".join(named))
    found = dict(merged.components)
    for key, value in answers.items():
        if value is None:
            found.pop(key, None)
        else:
            found[key] = str(value)
    return found


# The tasks


def parse_commit_map(text: str) -> dict[str, str]:
    """old sha -> new sha of a commit-map ``git filter-repo`` wrote (``old new`` lines under a header ``old new``); a
    commit the rewrite dropped maps to its zeros."""
    found = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and all(FULL_SHA.match(part) or GONE_SHA.match(part) for part in parts):
            found[parts[0]] = parts[1]
    return found


def mapped_sha(sha: str | None, commit_map: Mapping[str, str] | None) -> str | None:
    """``sha`` as the history holds it now: through ``commit_map`` (a short sha by the one key it starts), itself when
    the map does not name it; None when the rewrite dropped it."""
    if not sha or not commit_map:
        return sha
    found = commit_map.get(sha)
    if found is None and len(sha) >= MIN_PREFIX:
        keys = [key for key in commit_map if key.startswith(sha)]
        found = commit_map[keys[0]] if len(keys) == 1 else None
    if found is None:
        return sha
    return None if GONE_SHA.match(found) else found


def _overlaps(paths: Sequence[str], others: Iterable[str]) -> bool:
    """Whether a path of ``others`` is one of ``paths``, or in the directory of the skill one of them is a file of."""
    wanted = set(paths)
    skills = {found for found in map(replay.skill_dir, paths) if found}
    for other in others:
        if other in wanted or replay.skill_dir(other) in skills:
            return True
    return False


def _or_inf(value) -> float:
    found = _number(value)
    return float("inf") if found is None else found


def _light(task: Mapping, max_files: int, max_ms: int) -> bool:
    files, duration = _number(task.get("files")), _number(task.get("duration_ms"))
    return (files is None or files <= max_files) and (duration is None or duration <= max_ms)


def _replay_task(task: Mapping, commit_map: Mapping[str, str] | None) -> dict | None:
    commits = [mapped_sha(sha, commit_map) for sha in task.get("commits") or []]
    if not commits or any(sha is None for sha in commits):
        return None
    base = task.get("base_sha")
    base = mapped_sha(base, commit_map) if base else None
    if task.get("base_sha") and base is None:
        return None
    return {
        "kind": TASK_KINDS[0],
        "id": task["id"],
        "repo": task.get("repo"),
        "commits": commits,
        "base_sha": base,
        "files": task.get("files"),
        "duration_ms": task.get("duration_ms"),
    }


def _probe_paths(probe: Mapping) -> list[str]:
    paths = [path for path in probe.get("paths") or [] if isinstance(path, str)]
    found = probe.get("arms")
    arms = found if isinstance(found, dict) else {}
    for side in arms.values():
        paths += [path for path in (side or {}) if isinstance(path, str)]
    return paths


def _probe_task(probe: Mapping, repo: str | None, paths: Sequence[str]) -> dict | None:
    if probe.get("state", "active") != "active" or (repo is not None and probe.get("repo") != repo):
        return None
    named = _probe_paths(probe)
    if named and paths and not _overlaps(paths, named):
        return None
    return {"kind": TASK_KINDS[1], "id": probe["id"], "repo": probe.get("repo"), "base_sha": probe.get("base_sha")}


def light_tasks(
    tasks: Iterable[Mapping],
    probes: Iterable[Mapping] = (),
    *,
    repo: str | None = None,
    paths: Sequence[str] = (),
    commit_map: Mapping[str, str] | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_ms: int = DEFAULT_MAX_MS,
    limit: int = DEFAULT_TASKS,
    probe_limit: int = DEFAULT_PROBES,
) -> list[dict]:
    """The tasks an evolution of ``paths`` in ``repo`` measures its candidates on: at most ``limit`` replay tasks of
    the select split alone (rows of replay_tasks, each with ``files`` and ``duration_ms`` when known) whose commits
    change at most ``max_files`` files and whose step ran at most ``max_ms``, the lightest first, their commits read
    through ``commit_map``, those the rewrite dropped left out; then at most ``probe_limit`` active probes of ``repo``
    whose files meet ``paths`` (a probe that names none counts). A task of any other split is never read further than
    its split."""
    picked = []
    for task in tasks:
        if task.get("split") != SELECT:
            continue
        if (
            (repo is not None and task.get("repo") != repo)
            or task.get("unusable")
            or not _light(task, max_files, max_ms)
        ):
            continue
        found = _replay_task(task, commit_map)
        if found is not None:
            picked.append(found)
    picked.sort(key=lambda t: (_or_inf(t["files"]), _or_inf(t["duration_ms"]), str(t["id"])))
    found_probes = [item for item in (_probe_task(probe, repo, paths) for probe in probes) if item is not None]
    return picked[: max(limit, 0)] + found_probes[: max(probe_limit, 0)]


def task_ref(kind: str | None, task_id) -> str:
    """How a score names a task: ``replay:<id>`` or ``probe:<id>``."""
    if kind not in TASK_KINDS:
        raise EvolveProblem(f"a task is of kind {' or '.join(TASK_KINDS)}, not {kind!r}")
    return f"{kind}:{task_id}"


# The actionable side information of a candidate


def _bounded(value, depth: int) -> Any:
    if isinstance(value, str):
        return value[-ASI_TEXT:]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth <= 0:
        return None
    if isinstance(value, Mapping):
        return {str(key)[:200]: _bounded(item, depth - 1) for key, item in list(value.items())[:ASI_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth - 1) for item in list(value)[-ASI_ITEMS:]]
    return str(value)[-ASI_TEXT:]


def asi_of(traces, refs: Iterable[str] | None = None) -> dict[str, dict]:
    """task ref -> its ASI, each entry bounded (ASI_TEXT, ASI_ITEMS, ASI_DEPTH), from what a worker posted of a
    candidate's traces; an entry that is no object, or of a task not among ``refs`` when given, is left out."""
    if not isinstance(traces, Mapping):
        return {}
    wanted = set(refs) if refs is not None else None
    return {
        str(ref): _bounded(entry, ASI_DEPTH)
        for ref, entry in traces.items()
        if isinstance(entry, Mapping) and (wanted is None or ref in wanted)
    }


# The cache of the baseline arm


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def baseline_digest(components: Mapping[str, str | None]) -> str:
    """The sha256 of a baseline's components (path or axis -> whole text, None for no file), whatever their length."""
    found = {}
    for key, value in components.items():
        if value is not None and not isinstance(value, str):
            raise EvolveProblem(f"component {key} is no text")
        found[str(key)] = value
    return hashlib.sha256(_canonical(found)).hexdigest()


def arm_key(
    task_kind: str | None, task_id, base_sha: str | None, digest: str, runtime: str | None, model: str | None
) -> str:
    """The key of the result of the baseline arm of one task: the sha256 of (task kind, task, base commit, baseline
    digest, runtime, model). The same key is the same run, so its result is read again instead of run again."""
    if task_kind not in TASK_KINDS:
        raise EvolveProblem(f"a task is of kind {' or '.join(TASK_KINDS)}, not {task_kind!r}")
    if not runtime:
        raise EvolveProblem("the baseline arm's runtime is missing")
    return hashlib.sha256(_canonical([task_kind, str(task_id), base_sha, digest, runtime, model])).hexdigest()


# The paths an evolution may rewrite


def evolvable(repo: str, path: str, globs: Iterable[str]) -> bool:
    """Whether ``repo``'s ``path`` is a harness file (``replay.harness_path``) that one of ``globs``, the charter's
    ``evolve.evolvable_paths`` (``repo:glob`` for one repo), names."""
    return replay.harness_path(path) and any(tiers.matches(glob, repo, path) for glob in globs if isinstance(glob, str))
