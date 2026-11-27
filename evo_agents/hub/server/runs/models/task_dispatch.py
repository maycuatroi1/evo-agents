"""The request and response models of the plugin API's task runs and their pull requests
(``evo_agents.hub.server.task_dispatch``)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import task_runs
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.runs.models import OBJECT_NAME

RepoName = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")]
PULL_FIELDS = ("repo", "url", "number")  # what a task's plugin reads of each of its run's pull requests


class TaskRepo(StrictModel):
    """A repo of the project a task's run gets a worktree of, with the branch of origin that worktree starts from."""

    repo: RepoName
    base: str = Field(
        min_length=1,
        max_length=task_runs.MAX_BRANCH_CHARS,
        description="a branch of origin this repo's worktree starts from, in place of the task's base or the repo's "
        "default branch, which the draft pull request of its push goes into; the run fails when its origin lacks it",
    )


class TaskIn(StrictModel):
    task: str = Field(
        min_length=1,
        max_length=task_runs.MAX_TASK_BYTES,
        description=f"what the agent is to do, at most {task_runs.MAX_TASK_BYTES} bytes in UTF-8",
    )
    title: str | None = Field(
        None, max_length=task_runs.MAX_TITLE_CHARS, description="one line the run's list shows; default: the task's"
    )
    repo: str | None = Field(
        None,
        min_length=1,
        max_length=200,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        description="the repo of the project whose ``branch`` the run works on; with ``branch`` only",
    )
    branch: str | None = Field(
        None,
        min_length=1,
        max_length=task_runs.MAX_BRANCH_CHARS,
        description="a branch of ``repo`` that is no default branch, which the run checks out and pushes back to, "
        "never forced; default: task/<run> of each repo",
    )
    minutes: int | None = Field(
        None,
        ge=1,
        le=task_runs.MAX_MINUTES,
        description=f"the agent time the run may take, at most {task_runs.MAX_MINUTES}; default: no time cap",
    )
    repos: list[RepoName | TaskRepo] | None = Field(
        None,
        min_length=1,
        max_length=task_runs.MAX_REPOS,
        description="the repos of the project the run gets a worktree of, each once: a name, whose worktree starts "
        "from the task's base, else the repo's default branch, or {repo, base}, whose worktree starts from that base; "
        "default: every repo",
    )
    base: str | None = Field(
        None,
        min_length=1,
        max_length=task_runs.MAX_BRANCH_CHARS,
        description="a branch of origin each worktree of the run starts from instead of the repo's default branch, "
        "unless its item of repos names a base of its own, which the draft pull request of each branch it pushes goes "
        "into; the run fails when a repo's origin lacks it",
    )

    def repo_names(self) -> list[str] | None:
        """The names of the repos the task names, in its order, or None for every repo of the project."""
        if self.repos is None:
            return None
        return [entry if isinstance(entry, str) else entry.repo for entry in self.repos]

    def own_bases(self) -> dict[str, str]:
        """The repos of ``repos`` that name a base of their own: repo -> that base."""
        return {entry.repo: entry.base for entry in self.repos or () if isinstance(entry, TaskRepo)}

    def base_of(self, repo: str) -> str | None:
        """The branch of origin the worktree of ``repo`` starts from: the base the task names for it, else the task's
        ``base``, else None for the repo's default branch."""
        return self.own_bases().get(repo, self.base)

    @model_validator(mode="after")
    def _repo_with_branch(self) -> TaskIn:
        if (self.repo is None) != (self.branch is None):
            raise ValueError("a task names a repo and its branch together, or neither")
        names = self.repo_names()
        if names is not None and len(set(names)) != len(names):
            raise ValueError("a task names each of its repos once")
        if names is not None and self.repo is not None and self.repo not in names:
            raise ValueError(f"the repo whose branch the task names, {self.repo}, is one of its repos")
        own = self.own_bases().get(self.repo) if self.repo is not None else None
        if self.branch is not None and self.branch in (self.base, own):
            raise ValueError("a task's base is a branch it starts from, never the branch it pushes to")
        return self


class TaskBudget(BaseModel):
    max_usd: float = Field(description="what is left of the agent type's cap, the run's cost cap")
    max_seconds: int | None = Field(None, description="the agent time the task asked for; null: no time cap")


class Task(BaseModel):
    """A task a plugin handed one of its agent types, as its latest run has it: ids and states only."""

    task_id: int = Field(description="the run its dispatch made, which names its branch")
    run_id: int = Field(description="its latest run: a run its worker lost goes on in the next attempt")
    attempt: int
    project: str
    agent_type: str
    state: str
    branch: str
    pulls: list[dict] = Field(description="the draft pull requests the hub opened, {repo, url, number} each")
    failure_cause: str | None
    queued_at: datetime
    finished_at: datetime | None


class TaskQueued(Task):
    budget: TaskBudget
    spent_usd: float = Field(description=f"what its agent type's task runs cost in the last {task_runs.BUDGET_HOURS} h")
    daily_usd: float = Field(description="the agent type's cap, which the project's admins set")


class TaskPushed(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    branch: str = Field(
        min_length=1,
        max_length=255,
        description="the run's branch, task/<id>, or the branch its task names for the repo",
    )
    head: str = Field(pattern=OBJECT_NAME, description="the commit the branch has now")
    base: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        pattern=r"^[^\x00-\x20\x7f~^:?*\[\\]+$",
        description="the branch of origin the branch was made from; the base the task names goes first",
    )


class TaskPull(BaseModel):
    repo: str
    branch: str
    url: str | None = Field(description="the draft pull request, or the branch; null when the hub knows neither")
    number: int | None = Field(description="the pull request's number on GitHub")
    opened: bool = Field(description="the hub opened it now; false for one open already, or none")
    problem: str | None = Field(description="why GitHub opened none, when it refused")
