"""The probes of the Curator on the hub (``evo_agents.hub.probes`` holds their shape): one row of curator_probes each,
for a proposal and then for the change accepting it made, or for a merged change a review run or an admin gave one,
or for a rule of a core plugin's own (source plugin: ``ref`` the rule's key, ``arms`` the files each arm places, which
``evo_agents.hub.server.core_plugin_api`` adds and removes).

A probe names the repo of its task, the base commit (``commit``; none: the tip of the repo's default branch when it was
made), the task text the agent of a probe run is given, and the verify that exits 0 when that agent does not repeat the
mistake. Its task and verify go through ``evo_agents.hub.redact`` before they are stored, as a session's digest does:
what looks like a credential becomes ``***``. The verify is the answer key: ``verify_shown`` says who reads it (the
project's admins, never the agent of a run), and everyone else reads it null.

This module holds the models of a probe and its view (``view``); ``evo_agents.hub.server.probes.service.probes``
holds what other modules call (``store``, ``attach``, ``of_proposal``, ``verify_shown``) and
``evo_agents.hub.server.probes.routes.probes`` the routes (add, edit, retire, and requeue, which
``service.runs.requeue`` does).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evo_agents.hub import probes, replay, review
from evo_agents.hub.redact import redact
from evo_agents.hub.server.runs.models import MAX_ID

PROBE = "curator.probe"  # an admin wrote a probe: "<project> probe:<id> <add|edit|retire> change:<id>|proposal:<id>"

RepoName = Annotated[str, Field(pattern=review.REPO_NAME, max_length=probes.MAX_REPO_CHARS)]
Commit = Annotated[str, Field(pattern=replay.SHORT_SHA.pattern)]
Task = Annotated[str, Field(min_length=1, max_length=probes.MAX_TASK_BYTES)]
Verify = Annotated[str, Field(min_length=1, max_length=probes.MAX_VERIFY_CHARS)]


def _task(text: str) -> str:
    text = redact(text)
    if len(text.encode()) > probes.MAX_TASK_BYTES:
        raise ValueError(f"a probe's task is at most {probes.MAX_TASK_BYTES} bytes of UTF-8")
    return text


def _verify(text: str) -> str:
    text = redact(text.strip())
    why = probes.verify_problem(text)
    if why is not None:
        raise ValueError(f"the probe's verify {why}")
    return text


class ProbeIn(BaseModel):
    """A probe: the task an agent is given, from a base commit of a repo, and the verify that says it did not repeat
    the mistake."""

    model_config = ConfigDict(extra="forbid")

    repo: RepoName = Field(description="the repo of the project the task runs in")
    commit: Commit | None = Field(
        None, description="the base commit; none: the tip of the repo's default branch when the probe is made"
    )
    task: Task = Field(description="the task text the agent of a probe run is given, 16 KiB")
    verify: Verify = Field(
        description="one shell line, at most 2000 characters, that exits 0 when the agent does not repeat the "
        "mistake; its first program is a real command"
    )

    @model_validator(mode="after")
    def _checked(self):
        self.task = _task(self.task)
        self.verify = _verify(self.verify)
        return self


class ProbeEdit(BaseModel):
    """What an admin changes of a probe: any of its repo, commit, task and verify, at least one."""

    model_config = ConfigDict(extra="forbid")

    repo: RepoName | None = None
    commit: Commit | None = None
    task: Task | None = None
    verify: Verify | None = None

    @model_validator(mode="after")
    def _checked(self):
        if not self.model_fields_set:
            raise ValueError("an edit of a probe changes at least one of repo, commit, task and verify")
        for key in ("repo", "task", "verify"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"a probe keeps a {key}: give a new one, or leave it out")
        if self.task is not None:
            self.task = _task(self.task)
        if self.verify is not None:
            self.verify = _verify(self.verify)
        return self


class ProbeAdd(ProbeIn):
    """A probe an admin adds: to a change, or to a proposal (whose change, once accepting it made one, takes it)."""

    change_id: int | None = Field(None, ge=1, le=MAX_ID)
    proposal_id: int | None = Field(None, ge=1, le=MAX_ID)

    @model_validator(mode="after")
    def _one_target(self):
        if (self.change_id is None) == (self.proposal_id is None):
            raise ValueError("a probe is added to one change (change_id) or one proposal (proposal_id)")
        return self


class ReviewProbe(ProbeIn):
    """A probe the agent of a review run adds to a merged change that has none."""

    change_id: int = Field(ge=1, le=MAX_ID)
    replaces: int | None = Field(
        None,
        ge=1,
        le=MAX_ID,
        description="the change's active probe, not reproduced two weekly reports in a row, that this one replaces; "
        "the hub retires it",
    )


class ProbeRetire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(None, min_length=1, max_length=probes.MAX_NOTE_CHARS, description="why it is retired")


class Probe(BaseModel):
    id: int
    project: str
    proposal_id: int | None = Field(description="the proposal it came with, if any")
    change_id: int | None = Field(description="the change it probes; null until accepting its proposal made one")
    repo: str
    commit: str | None = Field(description="the base commit; null: the tip of the default branch when it was made")
    task: str
    verify: str | None = Field(description="the answer key: the project's admins alone read it, null for the others")
    state: Literal[probes.STATES]  # pyright: ignore[reportInvalidTypeForm]
    source: Literal[probes.SOURCES] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="who wrote it: its proposal, a review run, an admin, or a core plugin (plugin) for a rule of its own"
    )
    created_by: str | None
    created_run_id: int | None
    retired_at: datetime | None
    retired_note: str | None
    created_at: datetime
    updated_at: datetime
    replaces: int | None = Field(None, description="the probe, not reproduced two weeks in a row, this one replaced")
    plugin: str | None = Field(None, description="source plugin: the core plugin whose rule it measures")
    ref: str | None = Field(None, description="source plugin: the key of the plugin's rule it measures")


class ProbeList(BaseModel):
    probes: list[Probe] = Field(description="newest first")
    total: int


def view(project: str, row, *, shown: bool) -> Probe:
    data = {key: row._mapping[key] for key in Probe.model_fields if key != "project"}
    if not shown:
        data["verify"] = None
    return Probe(project=project, **data)
