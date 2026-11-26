"""The shapes of the evolve routes of the Curator (``evo_agents.hub.server.evolve``): what an owner sends to start an
evolution and reads of one, and what the worker of an evolve run reads and posts with the run's own key; the states of
an evolution, the actions of its audit rows and the limits of what it keeps."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import curator, evolve
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.runs.models import MAX_ID

EVOLVE_START = "curator.evolve_start"  # a member started an evolution: "<project> evolve:<id> target:<target>"
EVOLVE_RUN = "curator.evolve_run"  # an evolve run was queued: "<project> run:<id> evolve:<id>"
STATES = ("queued", "running", "done", "failed")  # curator_evolve_runs.state
OPEN_STATES = STATES[:2]
REASON_CHARS = 1000
MAX_LISTED = 100

MAX_PATHS = 50  # the files one evolution of a harness may rewrite
MAX_PARENTS = 2  # a merge has two parents, a mutation one
MAX_SCORES = 200  # task refs a candidate is scored on
MAX_HOLDOUT = 200
MAX_ITERATIONS = 1000
MAX_REASON = 1000
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{7,64}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Path = Annotated[str, Field(min_length=1, max_length=curator.MAX_PATH_CHARS, pattern=r"^[^\x00-\x1f\x7f]+$")]
Ref = Annotated[str, Field(pattern=r"^(replay|probe):[0-9]{1,19}$")]
Score = Annotated[float, Field(ge=-1e9, le=1e9)]
Id = Annotated[int, Field(ge=1, le=MAX_ID)]


def key_digest(key: str) -> str:
    """What the hub keeps of an evolve run's own key: its SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()


def reason_text(text: str | None) -> str | None:
    return None if text is None else text[:REASON_CHARS]


# The owner


class EvolveStart(StrictModel):
    """An evolution to start: of a harness's files in a repo (target git: ``repo`` and ``paths``), or of an agent of
    an agent system (target agent_config: ``system_id`` and ``agent``); ``proposal_id`` names the accepted proposal
    whose planned change takes the branch of a git evolution."""

    target: evolve.Target
    repo: Name | None = None
    paths: list[Path] = Field(default_factory=list, max_length=MAX_PATHS)
    system_id: Id | None = None
    agent: Name | None = Field(None, description="the node's id, or its profile's handle")
    proposal_id: Id | None = None

    @model_validator(mode="after")
    def _target(self) -> EvolveStart:
        if self.target == "git":
            if not self.repo or not self.paths or self.system_id is not None or self.agent is not None:
                raise ValueError("an evolution of target git takes repo and paths alone")
        elif self.system_id is None or not self.agent or self.repo is not None or self.paths:
            raise ValueError("an evolution of target agent_config takes system_id and agent alone")
        self.paths = list(dict.fromkeys(self.paths))
        return self


class FrontItem(BaseModel):
    candidate_id: int
    freq: int = Field(description="on how many tasks it reaches the ceiling of every candidate")


class EvolveTaskView(BaseModel):
    """A task of an evolution as its owner reads it: never its verify."""

    kind: evolve.TaskKind
    id: int
    ref: str
    repo: str | None = None


class CandidateView(BaseModel):
    id: int
    parents: list[int]
    origin: str
    iteration: int
    digest: str
    scores: dict[str, float]
    cost_usd: float | None
    turns: int | None
    on_front: bool
    components: dict[str, str | None] | None = Field(None, description="its whole files or axes; null in a list")
    asi: dict[str, dict] = Field(
        default_factory=dict,
        description="task ref -> its actionable side information: each failed check's exit code and output tail, the "
        "agent's failed tool calls, error and last message, each text cut to its end",
    )
    created_at: datetime


class EvolveView(BaseModel):
    id: int
    project: str
    run_id: int | None
    target: str
    repo: str | None
    paths: list[str]
    system_id: int | None
    agent: str | None
    state: str
    runtime: str | None
    model: str | None
    budget: dict
    tasks: list[EvolveTaskView]
    iterations: int
    frontier: list[FrontItem]
    best_candidate_id: int | None
    proposal_id: int | None
    change_id: int | None
    reason: str | None
    created_at: datetime
    updated_at: datetime
    candidates: list[CandidateView] | None = Field(None, description="its candidates; null in a list")


class EvolveList(BaseModel):
    project: str
    evolutions: list[EvolveView]


# The worker


class TaskInput(BaseModel):
    """A light task as the trusted daemon runs it, with its verify."""

    kind: evolve.TaskKind
    id: int
    ref: str
    base_sha: str | None
    prompt: str
    paths: list[str]
    verify: list[str]


class BudgetInput(BaseModel):
    iterations: int
    max_candidates: int
    max_cost_usd: float | None


class EvolveInputs(BaseModel):
    """What the worker of an evolve run reads: the evolution, its baseline, its light tasks (select split and active
    probes alone), the baseline arms the project's cache holds, its budget, runtime and model."""

    evolve_id: int
    project: str
    target: str
    repo: str | None
    base_sha: str | None = Field(description="null: the tip of the repo's default branch")
    branch: str | None = Field(description="target git: the branch the run pushes its best candidate to")
    paths: list[str]
    system_id: int | None
    agent: str | None
    baseline: dict[str, str | None] = Field(description="target agent_config: instructions and skill; git: {}")
    tasks: list[TaskInput]
    cached: dict[str, dict] = Field(description="evolve.arm_key -> the result of a baseline arm")
    budget: BudgetInput
    runtime: str
    model: str | None
    evolvable_paths: list[str]


class ArmIn(StrictModel):
    task_kind: evolve.TaskKind
    task_id: Id
    base_sha: Sha | None = None
    baseline_digest: Digest
    runtime: Name
    model: Name | None = None
    result: dict = Field(description="score, cost_usd, turns and trace of the baseline arm")


class ArmOut(BaseModel):
    key: str
    cached: bool = Field(description="true: the cache held this key already, and keeps what it held")


class CandidateIn(StrictModel):
    parents: list[Id] = Field(default_factory=list, max_length=MAX_PARENTS)
    origin: evolve.Origin
    iteration: int = Field(0, ge=0, le=MAX_ITERATIONS)
    components: dict[Path, str | None] = Field(min_length=1, max_length=MAX_PATHS)
    scores: dict[Ref, Score] = Field(default_factory=dict, max_length=MAX_SCORES)
    cost_usd: float | None = Field(None, ge=0, le=1e6)
    turns: int | None = Field(None, ge=0, le=10**6)
    traces: dict | list = Field(
        default_factory=dict, description="task ref -> its ASI (evolve_eval.short_trace), kept bounded (evolve.asi_of)"
    )


class CandidateOut(BaseModel):
    candidate_id: int
    frontier: list[FrontItem]


class HoldoutPair(StrictModel):
    baseline: Score
    candidate: Score


class ReportIn(StrictModel):
    """The end of an evolution. ``holdout`` (target agent_config) holds the scores of the baseline and of the best
    candidate on tasks of the holdout split, which the hub checks before it pins a new version of the profile."""

    state: Literal["done", "failed"]
    best_candidate_id: Id | None = None
    head_sha: Sha | None = None
    branch: Name | None = None
    reason: str | None = Field(None, max_length=MAX_REASON)
    holdout: dict[Ref, HoldoutPair] | None = Field(None, max_length=MAX_HOLDOUT)

    @model_validator(mode="after")
    def _done(self) -> ReportIn:
        if self.state == "done" and self.best_candidate_id is None:
            raise ValueError("a done evolution names its best_candidate_id")
        return self
