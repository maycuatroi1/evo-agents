"""The shapes and limits of the caller's overview, GET /v1/me/overview (``evo_agents.hub.server.overview``): what
the web's Home shows of the runs, decisions and projects the caller holds a grant on."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import runs
from evo_agents.hub.server.curator.models.charter import CuratorOverview

DAYS = 7  # the days of done_by_day and of the *_7d counts, today in UTC the last of them
MAX_ACTIVE = 20
MAX_RECENT = 10
MAX_DECISIONS = 20
MAX_AUTHOR_WAITING = 20
AUTHOR_WAITING_STATES = ("waiting", "parked")  # an author run's chat waits for its owner's reply (author.chat_status)
RUNNING_STATES = runs.CLOCK_STATES  # an agent at work, headless or driven by a person, or its verify commands
IN_FLIGHT = (*RUNNING_STATES, "waiting", "idle", "parked", "queued")  # in the order active_runs shows them
RECENT_STATES = (*runs.TERMINAL_STATES, "ended")  # ended is a local run's end
ENDED_COUNTED = ("done", "failed", "lost")  # the end states counts has a number for; a cancel is the owner's own


class OverviewCounts(BaseModel):
    waiting_on_you: int = Field(description="open decisions asked of you, which you answer; a wait is not one")
    running: int = Field(description=f"runs whose agent works: {', '.join(RUNNING_STATES)}")
    queued: int = Field(description="runs no worker has claimed yet")
    done_7d: int = Field(description=f"runs that ended done in the last {DAYS} days in UTC, today included")
    failed_7d: int = Field(description="runs that ended failed in the same days")
    lost_7d: int = Field(description="runs whose worker stopped extending the lease, in the same days")


class DayCount(BaseModel):
    day: date = Field(description="a day in UTC")
    done: int = Field(description="runs that ended done that day")


class OverviewRun(BaseModel):
    id: int
    kind: Literal[runs.SHOWN_KINDS]  # pyright: ignore[reportInvalidTypeForm]
    project: str
    plan_id: str
    plan_title: str | None = Field(description="the plan's title as the hub holds it now")
    step_key: str | None = Field(description="null for a plan run")
    title: str | None = Field(description="the step's title when the run was dispatched; the plan's for a plan run")
    state: Literal[runs.SHOWN_STATES]  # pyright: ignore[reportInvalidTypeForm]
    dispatched_by: str = Field(description="the login of the member who dispatched it, its owner")
    worker_id: int | None = Field(description="the worker that claimed it; null while it is queued")
    worker: str | None = Field(description="that worker's name")
    runtime: str = Field(description="any until a worker claims the run; a plugin's runtime is <plugin>/<name>")
    model: str | None
    steps_total: int | None = Field(description="the steps of a plan run's plan; null for a run of one step")
    steps_done: int | None = Field(description="a plan run's steps the plan holds done now; null for a run of one step")
    run_seconds: int = Field(description="the agent time the run has used, as the hub last counted it")
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None = Field(description="why the run failed, was lost or ended")
    machine: str | None = Field(None, description="the machine of a local run's session; null for any other run")
    cwd: str | None = Field(None, description="the session's working directory; null for any other run")
    claude_email: str | None = Field(None, description="the Claude account of a local run; null for any other run")
    claude_profile: str | None = Field(None, description="the claude-profiles profile of a local run, when known")
    last_activity_at: datetime | None = Field(None, description="the last sign of life of a local run's session")


class OverviewDecision(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the plan run or author run the decision belongs to")
    run_state: Literal[runs.SHOWN_STATES]  # pyright: ignore[reportInvalidTypeForm]
    plan_id: str | None = Field(
        description="the run's plan when its agent asked; null for an author run that had put no plan yet, and a "
        "local run on none"
    )
    plan_title: str | None
    step_key: str | None = Field(description="the step it is about, when the agent named one")
    category: Literal[runs.SHOWN_DECISION_CATEGORIES]  # pyright: ignore[reportInvalidTypeForm]
    question: str
    owner: str = Field(description="the login of the run's owner, who dispatched it")
    asked_of: str = Field(
        description="the login of the member it is asked of, who answers it: the person the agent asked, else the "
        "run's owner (the principal of an autonomy gate's run)"
    )
    yours: bool = Field(description="whether it is asked of you")
    asked_at: datetime
    parks_at: datetime | None = Field(
        description="when the run parks for want of an answer: EVO_HUB_DECISION_WAIT_SECONDS after it started "
        "waiting, or when it parked; null while its agent still works"
    )


class OverviewAuthorWait(BaseModel):
    id: int
    project: str
    plan_id: str | None = Field(description="the plan the run wrote or revises; null before its first put")
    plan_title: str | None = Field(description="that plan's title as the hub holds it now")
    title: str | None = Field(description="the run's title, from the start of its request")
    state: Literal[AUTHOR_WAITING_STATES]  # pyright: ignore[reportInvalidTypeForm]
    worker: str | None = Field(description="the worker the run is on")
    waiting_since: datetime | None = Field(description="when its agent's turn ended, while waiting")
    parked_at: datetime | None = Field(description="when it was parked, for want of a reply")
    message: str | None = Field(description="the agent's last message of the chat; null when it said nothing yet")


class OverviewProject(BaseModel):
    name: str
    role: str = Field(description="your role on the project")
    max_level: str = Field(description="the highest label level your grant reaches")
    repos: int = Field(description="the repos registered for the project")
    active_plans: int = Field(description="its plans in the active area that you can see")
    open_decisions: int = Field(description="open decisions of the plans you can see, yours or not")
    curator: CuratorOverview | None = Field(
        None, description="where the project's Curator stands; null while the project has no charter"
    )


class Overview(BaseModel):
    counts: OverviewCounts
    done_by_day: list[DayCount] = Field(
        min_length=DAYS, max_length=DAYS, description=f"the last {DAYS} days in UTC, oldest first, today last"
    )
    active_runs: list[OverviewRun] = Field(
        max_length=MAX_ACTIVE,
        description=f"at most {MAX_ACTIVE} runs in {', '.join(IN_FLIGHT)}: those whose agent works first, then "
        "waiting, idle, parked and queued, the newest first within each",
    )
    recent_runs: list[OverviewRun] = Field(
        max_length=MAX_RECENT, description=f"the {MAX_RECENT} runs that ended last, the last first"
    )
    open_decisions: list[OverviewDecision] = Field(
        max_length=MAX_DECISIONS,
        description=f"at most {MAX_DECISIONS}: those asked of you first, then the oldest first",
    )
    author_waiting: list[OverviewAuthorWait] = Field(
        [],
        max_length=MAX_AUTHOR_WAITING,
        description=f"at most {MAX_AUTHOR_WAITING} of your author runs whose chat waits for your reply, the latest "
        "first",
    )
    projects: list[OverviewProject] = Field(description="the projects you hold a grant on, by name")
