"""The tasks of the bench: read from the cache, kept once across sets, split by repo, sampled, asked as questions.

Parts. Each repo falls in one part by the SHA-256 of its lowercased name (``repo_split``), the same in every set and
on every machine: TUNE_SHARE in ``tune``, HOLDOUT_SHARE in ``holdout``, the rest in ``test``. A repo of LocBench
(``LOCBENCH_REPOS``) that the hash puts in ``tune`` is in ``holdout`` instead, in every set: LocBench is scored only
on repos ranking was never tuned on. ``test`` is sealed: only ``run --sealed`` reads it.

Dedup. ContextBench and SWE-Explore both draw on SWE-bench Verified and Pro, and LocBench and MuLocBench on the same
GitHub repos; a task whose ``key`` an earlier set of DEDUPE_ORDER holds is left out of the later one. The smaller sets
come first, so they keep their tasks. Reading a set needs the sets before it fetched, so the dedup does not depend on
what this machine happened to fetch.

Sampling. ``sample`` takes a fixed share of each repo (largest remainder, at least one task of a repo while the size
allows), the tasks of a repo ordered by the SHA-256 of the seed and their id: the same seed gives the same sample.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from evo_agents.kg.bench.datasets import (
    CODE_BENCHES,
    CONTEXTBENCH,
    LOCBENCH,
    MULOCBENCH,
    SWE_EXPLORE,
    BenchError,
    data_dir,
)
from evo_agents.kg.bench.locbench_repos import LOCBENCH_REPOS
from evo_agents.kg.evaluate import Question

TUNE, HOLDOUT, TEST = "tune", "holdout", "test"
PARTS = (TUNE, HOLDOUT, TEST)
TUNE_SHARE, HOLDOUT_SHARE = 0.5, 0.25  # of the repos; test has the rest. Changing them moves repos between parts.
DEDUPE_ORDER = (SWE_EXPLORE, LOCBENCH, MULOCBENCH, CONTEXTBENCH)
SOURCE_ID = "repo"  # the source of a bench graph: its node ids read symbol:repo:PATH::QUALNAME


@dataclass(frozen=True)
class Task:
    bench: str
    id: str
    key: str
    repo: str
    commit: str
    language: str
    question: str
    files: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()  # path::qualname
    lines: tuple[tuple[str, int, int], ...] = ()

    @property
    def split(self) -> str:
        return repo_split(self.repo)

    def as_question(self) -> Question:
        """The task as one `kg eval` question: the issue verbatim, the answers as the graph names them."""
        return Question(
            id=self.id,
            text=self.question,
            type=self.bench,
            files=tuple((None, path) for path in self.files),
            nodes=tuple(f"symbol:{SOURCE_ID}:{node}" for node in self.nodes),
            lines=tuple((None, path, first, last) for path, first, last in self.lines),
        )

    def eval_line(self) -> dict:
        """The question as a line of a `kg eval` set."""
        q = self.as_question()
        line = {"id": q.id, "question": q.text, "type": q.type, "files": list(self.files), "nodes": list(q.nodes)}
        if self.lines:
            line["lines"] = [f"{path}:{first}-{last}" for path, first, last in self.lines]
        return line


def _fraction(text: str) -> float:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") / 2**64


def repo_split(repo: str) -> str:
    """The part a repo's tasks fall in, in every set: by the hash of its name, LocBench's repos never in tune."""
    name = repo.strip().lower()
    u = _fraction(f"kg-bench:{name}")
    part = TUNE if u < TUNE_SHARE else HOLDOUT if u < TUNE_SHARE + HOLDOUT_SHARE else TEST
    return HOLDOUT if part == TUNE and name in LOCBENCH_REPOS else part


def read_tasks(cache: Path, bench: str) -> list[Task]:
    path = data_dir(cache, bench) / "tasks.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BenchError(f"{bench} is not fetched: run `evo-agents kg bench fetch {bench}`") from None
    tasks, seen = [], set()
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        tasks.append(
            Task(
                bench=row["bench"],
                id=row["id"],
                key=row["key"],
                repo=row["repo"],
                commit=row["commit"],
                language=row.get("language") or "other",
                question=row["question"],
                files=tuple(row.get("files") or ()),
                nodes=tuple(row.get("nodes") or ()),
                lines=tuple((p, int(a), int(b)) for p, a, b in row.get("lines") or ()),
            )
        )
    return tasks


def load_tasks(cache: Path, bench: str) -> tuple[list[Task], int]:
    """The tasks of ``bench`` once the sets before it in DEDUPE_ORDER took theirs, and how many that left out."""
    if bench not in CODE_BENCHES:
        raise BenchError(f"no code bench {bench!r}: one of {', '.join(CODE_BENCHES)}")
    taken: set[str] = set()
    for earlier in DEDUPE_ORDER[: DEDUPE_ORDER.index(bench)]:
        if not (data_dir(cache, earlier) / "tasks.jsonl").is_file():
            raise BenchError(
                f"{bench} is kept apart from {earlier} by instance: fetch {earlier} too "
                f"(`evo-agents kg bench fetch {earlier}`)"
            )
        taken.update(t.key for t in read_tasks(cache, earlier))
    kept, dropped = [], 0
    for task in read_tasks(cache, bench):
        if task.key in taken:
            dropped += 1
            continue
        taken.add(task.key)
        kept.append(task)
    return kept, dropped


def sample(tasks: list[Task], size: int, seed: int) -> list[Task]:
    """``size`` of ``tasks``, a fixed share of each repo (see the module); all of them when ``size`` is not less."""
    if size >= len(tasks):
        return list(tasks)
    groups: dict[str, list[Task]] = {}
    for task in tasks:
        groups.setdefault(task.repo, []).append(task)
    total = len(tasks)
    exact = {repo: size * len(group) / total for repo, group in groups.items()}
    quota = {repo: math.floor(share) for repo, share in exact.items()}
    if size >= len(groups):
        for repo in groups:
            quota[repo] = max(quota[repo], 1)
    left = size - sum(quota.values())
    for repo in sorted(groups, key=lambda r: (-(exact[r] - math.floor(exact[r])), _fraction(f"{seed}:{r}"))):
        if left <= 0:
            break
        if quota[repo] < len(groups[repo]):
            quota[repo] += 1
            left -= 1
    while left < 0:  # the minimum of one a repo took more than size: take back from the largest quotas
        repo = max(sorted(quota), key=quota.get)
        quota[repo] -= 1
        left += 1
    picked: list[Task] = []
    for repo, group in groups.items():
        ordered = sorted(group, key=lambda t: _fraction(f"{seed}:{t.bench}:{t.id}"))
        picked += ordered[: quota[repo]]
    order = {id(t): i for i, t in enumerate(tasks)}
    return sorted(picked, key=lambda t: order[id(t)])
