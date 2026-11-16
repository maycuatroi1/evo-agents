"""The loop of an evolve run (``evo_agents.worker.runner.evolve``): a population of candidates of a harness kept on the
Pareto front of the tasks they were measured on, after GEPA and a meta-harness search, on the operators of
``evo_agents.hub.evolve``.

The seed is the baseline. Its score on each task is the task's baseline arm: read from the hub's arm cache by
``evolve.arm_key`` (task kind, task, base commit, digest of the baseline, runtime, model) when the hub has it, else run
once and posted to the cache. Then each iteration, until the budget (iterations, candidates, cost) is spent, either
merges two candidates of the front (every ``merge_every``-th iteration, when the front holds a pair not merged yet whose
merge brings something) or mutates one: a parent drawn by how often it is on the front (``front_freq``,
``pick_parent``), its whole files laid out in the tree (``evolve_workspace``) and rewritten by the proposer agent, which
reads the tree's reflection data. A merge takes ``lca_merge`` over the pair's nearest common ancestor; the files both
changed apart go to the agent (``synthesis_request``, ``apply_synthesis``). Each new candidate is measured on every task
(``evolve_eval.evaluate``), posted to the hub with each task's ASI (``short_trace``), its traces written, and the front
pruned (``prune_all``). A mutation's prompt holds its parent's ASI of each task it did not pass. A candidate the
same as one measured already, or one the proposer left unchanged, is not measured again. The loop ends with the best
candidate (``evolve.best``).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from evo_agents.hub import evolve
from evo_agents.worker.runner import evolve_workspace as workspace
from evo_agents.worker.runner.evolve_eval import Measured, Seams, evaluate, placed, ref_of, short_trace, usable

DEFAULT_ITERATIONS = 6
DEFAULT_CANDIDATES = 12
MERGE_EVERY = 3  # every third iteration tries a merge of two front candidates before a mutation


@dataclass
class Candidate:
    """A candidate measured: its hub id, whole components, parents, origin, iteration, scores, cost and turns, and the
    ASI of each task (``short_trace``)."""

    id: str
    components: dict[str, str]
    parents: list[str]
    origin: str
    iteration: int
    scores: dict[str, float]
    cost_usd: float | None
    turns: int | None
    asi: dict[str, dict] = field(default_factory=dict)


class Hub(Protocol):
    """The hub's side of an evolution, the run's key held by whoever implements it."""

    async def arm(self, body: dict) -> dict: ...

    async def candidate(self, body: dict) -> dict: ...


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _whole(value, default: int) -> int:
    found = _number(value)
    return int(found) if found is not None and found >= 0 else default


def _sum(values) -> float | None:
    found = [value for value in values if value is not None]
    return round(sum(found), 6) if found else None


class Evolution:
    """One evolution over ``inputs`` (what GET /v1/worker/runs/{id}/evolve answered), through ``hub`` and ``seams``,
    its tree at ``tree``."""

    def __init__(
        self,
        inputs: Mapping,
        hub: Hub,
        seams: Seams,
        tree: workspace.Tree,
        *,
        rng: random.Random | None = None,
        merge_every: int = MERGE_EVERY,
        note: Callable[[str], None] | None = None,
        check: Callable[[], None] | None = None,
    ):
        self.inputs = inputs
        self.hub, self.seams, self.tree = hub, seams, tree
        self.target = inputs.get("target") or evolve.TARGETS[0]
        self.tasks = [task for task in inputs.get("tasks") or [] if isinstance(task, Mapping) and usable(task)]
        self.baseline = {str(k): v for k, v in (inputs.get("baseline") or {}).items() if isinstance(v, str)}
        self.digest = evolve.baseline_digest(self.baseline)
        budget = inputs.get("budget") or {}
        self.iterations = _whole(budget.get("iterations"), DEFAULT_ITERATIONS)
        self.max_candidates = _whole(budget.get("max_candidates"), DEFAULT_CANDIDATES)
        self.max_cost = _number(budget.get("max_cost_usd"))
        self.spent = 0.0
        self.pool: dict[str, Candidate] = {}
        self.digests: set[str] = set()
        self.merged_pairs: set[frozenset[str]] = set()
        self.rng = rng or random.Random()
        self.merge_every = merge_every
        self.note = note or (lambda text: None)
        self.check = check or (lambda: None)

    # The loop

    async def run(self) -> Candidate | None:
        """Seed, then iterate until the budget is spent; the best candidate."""
        await self.seed()
        for number in range(1, self.iterations + 1):
            if self.exhausted():
                break
            self.check()
            merged = False
            if self.merge_every and number % self.merge_every == 0:
                merged = await self.merge(number)
            if not merged:
                await self.mutate(number)
        return self.best()

    def exhausted(self) -> bool:
        """Whether the budget's candidates or cost are spent."""
        if len(self.pool) >= max(self.max_candidates, 1):
            self.note(f"The evolution measured {len(self.pool)} candidate(s), its budget: it stops.")
            return True
        if self.max_cost is not None and self.spent >= self.max_cost:
            self.note(f"The evolution spent ${self.spent:.2f} of ${self.max_cost:.2f}: it stops.")
            return True
        return False

    def spend(self, cost: float | None) -> None:
        self.spent += cost or 0.0

    # The seed and its baseline arms

    async def seed(self) -> Candidate:
        """The baseline as candidate 0: each task's score from its baseline arm, cached or run once."""
        measured = []
        for task in self.tasks:
            self.check()
            measured.append(await self.baseline_arm(task))
        return await self.post([], evolve.ORIGINS[0], 0, dict(self.baseline), measured)

    def arm_key(self, task: Mapping) -> str:
        runtime, model = self.inputs.get("runtime"), self.inputs.get("model")
        return evolve.arm_key(task.get("kind"), task.get("id"), task.get("base_sha"), self.digest, runtime, model)

    async def baseline_arm(self, task: Mapping) -> Measured:
        """The baseline's measure on ``task``: the hub's cached result of its arm, else the arm run now and posted."""
        cached = (self.inputs.get("cached") or {}).get(self.arm_key(task))
        if isinstance(cached, Mapping) and _number(cached.get("score")) is not None:
            kept = cached.get("trace")
            trace = {**(kept if isinstance(kept, Mapping) else {}), "task": ref_of(task), "cached": True}
            return Measured(ref_of(task), float(cached["score"]), _number(cached.get("cost_usd")), None, trace)
        found = await evaluate(self.seams, task, placed(self.baseline, self.baseline), self.eval_name(0, task))
        self.spend(found.cost_usd)
        result = {"score": found.score, "cost_usd": found.cost_usd, "turns": found.turns, "trace": found.trace}
        body = {
            "task_kind": task.get("kind"),
            "task_id": task.get("id"),
            "base_sha": task.get("base_sha"),
            "baseline_digest": self.digest,
            "runtime": self.inputs.get("runtime"),
            "model": self.inputs.get("model"),
            "result": result,
        }
        await self.hub.arm(body)
        return found

    @staticmethod
    def eval_name(number: int, task: Mapping, origin: str = "baseline") -> str:
        return f"{number}-{origin}-{workspace.safe_name(ref_of(task))}"

    # A candidate measured and posted

    async def measure(self, number: int, origin: str, components: Mapping[str, str]) -> list[Measured]:
        found = []
        for task in self.tasks:
            self.check()
            measured = await evaluate(
                self.seams, task, placed(self.baseline, components), self.eval_name(number, task, origin)
            )
            self.spend(measured.cost_usd)
            found.append(measured)
        return found

    async def post(
        self, parents: list[str], origin: str, number: int, components: dict[str, str], measured: list[Measured]
    ) -> Candidate:
        """POST the candidate, write its traces and its line of the summary, and prune the front."""
        scores = {item.ref: item.score for item in measured}
        cost = _sum(item.cost_usd for item in measured)
        turns = _sum(item.turns for item in measured)
        body = {
            "parents": parents,
            "origin": origin,
            "iteration": number,
            "components": components,
            "scores": scores,
            "cost_usd": cost,
            "turns": int(turns) if turns is not None else None,
            "traces": {item.ref: short_trace(item.trace) for item in measured},
        }
        answer = await self.hub.candidate(body)
        found = Candidate(
            str(answer["candidate_id"]),
            components,
            parents,
            origin,
            number,
            scores,
            cost,
            body["turns"],
            body["traces"],
        )
        self.pool[found.id] = found
        self.digests.add(evolve.baseline_digest(components))
        for item in measured:
            self.tree.write_trace(found.id, item.ref, item.trace)
        front = self.frontier()
        self.tree.append_summary(
            {
                "iteration": number,
                "candidate_id": found.id,
                "parents": parents,
                "origin": origin,
                "scores": scores,
                "total": sum(scores.values()),
                "cost_usd": cost,
                "turns": found.turns,
                "on_front": any(row["candidate_id"] == found.id for row in front),
                "components": sorted(components),
            }
        )
        self.note(f"Candidate {found.id} ({origin}, iteration {number}): {sum(scores.values()):g} of {len(scores)}.")
        return found

    def scores(self) -> dict[str, dict[str, float]]:
        return {cid: cand.scores for cid, cand in self.pool.items()}

    def costs(self) -> dict[str, dict]:
        return {cid: {"cost_usd": cand.cost_usd, "turns": cand.turns} for cid, cand in self.pool.items()}

    def freq(self) -> dict[str, int]:
        """survivor of the front -> on how many tasks it is best."""
        tasks = [ref_of(task) for task in self.tasks]
        survivors = evolve.prune_all(self.scores(), self.costs(), tasks)
        return evolve.front_freq(self.scores(), survivors, tasks)

    def frontier(self) -> list[dict]:
        """The front now, as frontier.json holds it, written there."""
        rows = [
            {
                "candidate_id": cid,
                "freq": freq,
                "scores": self.pool[cid].scores,
                "cost_usd": self.pool[cid].cost_usd,
                "turns": self.pool[cid].turns,
                "origin": self.pool[cid].origin,
                "parents": self.pool[cid].parents,
            }
            for cid, freq in self.freq().items()
        ]
        self.tree.write_frontier(rows)
        return rows

    def best(self) -> Candidate | None:
        found = evolve.best(self.scores(), self.costs(), [ref_of(task) for task in self.tasks])
        return self.pool.get(found) if found is not None else None

    def new(self, components: Mapping[str, str], number: int, why: str) -> bool:
        """Whether ``components`` make a candidate not measured yet; a line of the summary says why not otherwise."""
        if components and evolve.baseline_digest(components) not in self.digests:
            return True
        self.tree.append_summary({"iteration": number, "skipped": why})
        self.note(f"Iteration {number}: {why}, nothing measured.")
        return False

    # A mutation

    async def mutate(self, number: int) -> Candidate | None:
        """A parent drawn from the front, its files rewritten by the proposer, the child measured and posted."""
        parent = self.pool[evolve.pick_parent(self.freq(), self.rng)]
        folder = self.tree.candidate_folder(number, self.target, parent.id, parent.scores, parent.components)
        files = workspace.component_files(self.target, parent.components).values()
        failed = {ref: asi for ref, asi in parent.asi.items() if parent.scores.get(ref, 0.0) < 1.0}
        prompt = workspace.proposer_prompt(
            target=self.target, iteration=number, parent=parent.id, files=files, asi=failed
        )
        session = await self.seams.agent(self.tree.root, prompt)
        self.spend(session.cost_usd)
        child = workspace.read_components(folder, self.target, parent.components)
        why = "the proposer changed nothing" if child == parent.components else "the proposer's files are a candidate's"
        if not self.new(child, number, why):
            return None
        measured = await self.measure(number, evolve.ORIGINS[1], child)
        return await self.post([parent.id], evolve.ORIGINS[1], number, child, measured)

    # A merge

    def merge_pair(self) -> tuple[str, str] | None:
        """Two candidates of the front, the most often best first, not merged yet and neither the other's ancestor."""
        freq = self.freq()
        ranked = sorted(freq, key=lambda cid: (-freq[cid], cid))
        parents = {cid: cand.parents for cid, cand in self.pool.items()}
        for index, one in enumerate(ranked):
            for other in ranked[index + 1 :]:
                if frozenset((one, other)) in self.merged_pairs:
                    continue
                if other in evolve.ancestry(parents, one) or one in evolve.ancestry(parents, other):
                    continue
                return one, other
        return None

    async def merge(self, number: int) -> bool:
        """Merge a pair of the front over its nearest common ancestor, the conflicts by the agent; whether a merged
        candidate was measured."""
        pair = self.merge_pair()
        if pair is None:
            return False
        one, other = pair
        self.merged_pairs.add(frozenset(pair))
        ancestor = evolve.lca({cid: cand.parents for cid, cand in self.pool.items()}, one, other)
        base = self.pool[ancestor].components if ancestor is not None else self.baseline
        merged = evolve.lca_merge(base, self.pool[one].components, self.pool[other].components)
        if not merged.useful:
            return False
        components = merged.components
        if merged.conflicts:
            components = await self.synthesize(number, merged)
            if components is None:
                return False
        found = {key: value for key, value in components.items() if value is not None}
        if not self.new(found, number, f"the merge of {one} and {other} is a candidate already"):
            return False
        self.note(f"Iteration {number}: merging {one} and {other} over {ancestor or 'the baseline'}.")
        measured = await self.measure(number, evolve.ORIGINS[2], found)
        await self.post([one, other], evolve.ORIGINS[2], number, found, measured)
        return True

    async def synthesize(self, number: int, merged: evolve.Merged) -> dict[str, str | None] | None:
        """The components of ``merged`` with its conflicts written by the agent; None when it left them unanswered."""
        request = evolve.synthesis_request(merged, target=self.target)
        keys = [item.key for item in merged.conflicts]
        folder = self.tree.merge_folder(number, self.target, request)
        prompt = workspace.merge_prompt(target=self.target, iteration=number, keys=keys)
        session = await self.seams.agent(self.tree.root, prompt)
        self.spend(session.cost_usd)
        answers = workspace.read_answers(folder, self.target, keys)
        try:
            return evolve.apply_synthesis(merged, answers)
        except evolve.EvolveProblem as exc:
            self.note(f"Iteration {number}: the merge's answers were not taken ({exc}).")
            return None
