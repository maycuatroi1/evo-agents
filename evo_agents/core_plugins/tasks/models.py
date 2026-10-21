"""The API of the plugin tasks as Pydantic models: what its routes take and answer, and how a task, a link, a sprint
and an import become an answer. Every model is named ``Tasks...`` in the hub's OpenAPI document.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evo_agents.core_plugins.tasks import GROUPS
from evo_agents.core_plugins.tasks.blockers import KINDS, WAITING_KINDS, BlockerRow, Waiting
from evo_agents.core_plugins.tasks.imports import Plan, TasksFile
from evo_agents.core_plugins.tasks.links import LinkView
from evo_agents.core_plugins.tasks.settings import Settings
from evo_agents.core_plugins.tasks.store import CommentRow, EventRow, People, SprintRow, TaskRow
from evo_agents.hub.server.projects import LabelIn
from evo_agents.hub.tables_tasks import (
    LINK_KINDS,
    MAX_COMMENT_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_GOAL_CHARS,
    MAX_REASON_CHARS,
    MAX_SPRINT_NAME_CHARS,
    MAX_TITLE_CHARS,
    MAX_WAITING_CHARS,
    PRIORITIES,
    SPRINT_STATES,
)

MAX_LINKS = 100  # of one kind, added at once
MAX_PEOPLE = 50  # in one slot
Login = Field(min_length=1, max_length=100)
SprintName = Field(min_length=1, max_length=MAX_SPRINT_NAME_CHARS)


class TasksStatus(BaseModel):
    key: str
    name: str
    group: Literal[GROUPS] = Field(description="what the status means, whatever the project names it")


class TasksSettings(BaseModel):
    """The plugin's config in the project, as a task is written against it."""

    prefix: str = Field(description="what a task's number is shown after, as T in T-42")
    statuses: list[TasksStatus] = Field(description="in the order of the board; a new task takes the first")
    types: list[str] = Field(description="a new task takes the first")
    on_steps_done: str | None = Field(description="the status a task moves to once every plan step it links is done")
    srs: bool = Field(description="the project enables the plugin srs, so a task links to its items")


class TasksRoles(BaseModel):
    ba: list[str]
    qa: list[str]
    dev: list[str]


class TasksItem(BaseModel):
    """A task of a project, as a list or a board shows it."""

    number: int
    ref: str = Field(description="the number as shown, after the config's prefix: T-42")
    type: str
    title: str
    status: str
    status_group: Literal[GROUPS] | None = Field(description="None for a status the config no longer lists")
    priority: Literal[PRIORITIES]
    due_on: date | None
    sprint: str | None
    parent: str | None = Field(description="the ref of the task it belongs to")
    assignees: list[str]
    roles: TasksRoles
    steps_done: int = Field(description="how many of the plan steps it links are done")
    steps_total: int = Field(description="how many plan steps it links that the caller sees")
    codes: list[str] = Field(description="the codes of the SRS items it links to that the caller sees, as linked")
    label: dict = Field(description="the label the task carries")
    blocked: bool = Field(description="it has an open blocker: one set by hand, or an open decision of its runs")
    created_by: str
    created_at: datetime
    updated_at: datetime


class TasksRun(BaseModel):
    id: int
    state: str


class TasksLink(BaseModel):
    """What a task links to, as it is now; only the fields of its kind are set."""

    id: int
    kind: Literal[LINK_KINDS]
    hidden: bool = Field(description="its target is above what the caller may read: nothing of it is shown")
    code: str | None = Field(description="srs_item: the item's code now")
    item_kind: str | None = Field(description="srs_item: use_case, business_rule, screen, nfr or cbr")
    item_title: str | None = Field(description="srs_item: its title in the latest version that has it")
    document: str | None = Field(description="srs_item: the slug of its document")
    retired: bool = Field(description="srs_item: dropped from its document since")
    plan_id: str | None
    step: str | None
    step_title: str | None
    step_status: str | None = Field(description="plan_step: the step's status in the plan now")
    runs: list[TasksRun] = Field(description="plan_step: its latest runs, the newest first")
    run_id: int | None
    run_kind: str | None
    run_state: str | None
    url: str | None = Field(description="pull: the pull or merge request")
    created_by: str
    created_at: datetime


class TasksEvent(BaseModel):
    actor: str | None = Field(description="who changed it; None for the hub")
    field: str
    from_value: str | None
    to_value: str | None
    cause: str
    at: datetime


class TasksComment(BaseModel):
    id: int
    author: str
    body: str
    created_at: datetime


class TasksBlocker(BaseModel):
    """What a task waits on: a blocker set by hand, or an open decision of a run that belongs to the task."""

    id: int | None = Field(description="the blocker's id for PATCH; None for an open decision")
    kind: Literal[KINDS] = Field(description="manual: set with POST blockers; decision: an open decision of a run")
    reason: str = Field(description="why; a decision's question")
    waiting_kind: Literal[WAITING_KINDS] = Field(description="who or what it waits on: a member, a task or a text")
    waiting_on: str = Field(description="the member's login, the task's ref, or the text")
    since: datetime = Field(description="when it was set; when a decision was asked")
    set_by: str | None = Field(description="who set it; None for a decision")
    cleared_at: datetime | None
    cleared_by: str | None
    decision_id: int | None = Field(description="decision: the decision to answer")
    run_id: int | None = Field(description="decision: the run that asked it")


class TasksItemDetail(TasksItem):
    """A task whole: its description, links, blockers, history and comments."""

    description_md: str
    outcome_md: str = Field(
        description="what the task came to, markdown; pm reads it before the description's outcome section"
    )
    external_ref: dict | None = Field(description="where an import took it from, and what it named it could not link")
    links: list[TasksLink]
    blockers: list[TasksBlocker] = Field(description="every open blocker, those set by hand first")
    events: list[TasksEvent]
    comments: list[TasksComment]


class TasksLinksIn(BaseModel):
    """Links to add."""

    use_cases: list[str] = Field(default_factory=list, max_length=MAX_LINKS, description="codes of SRS items")
    steps: list[str] = Field(default_factory=list, max_length=MAX_LINKS, description="plan#step, as order-cancel#2")
    runs: list[int] = Field(default_factory=list, max_length=MAX_LINKS)
    pulls: list[str] = Field(default_factory=list, max_length=MAX_LINKS, description="URLs of pull or merge requests")


class TasksRolesIn(BaseModel):
    """The people of each role given, each list whole; a role left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    ba: list[str] | None = Field(None, max_length=MAX_PEOPLE)
    qa: list[str] | None = Field(None, max_length=MAX_PEOPLE)
    dev: list[str] | None = Field(None, max_length=MAX_PEOPLE)

    def given(self) -> dict[str, list[str]]:
        return {role: logins for role, logins in self.model_dump().items() if logins is not None}


class TasksItemIn(BaseModel):
    """A new task; its type and status are the config's first when left out."""

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    description_md: str = Field("", max_length=MAX_DESCRIPTION_CHARS)
    outcome_md: str = Field("", max_length=MAX_DESCRIPTION_CHARS)
    type: str | None = Field(None, max_length=40)
    status: str | None = Field(None, max_length=40)
    priority: Literal[PRIORITIES] = "medium"
    due_on: date | None = None
    sprint: str | None = Field(None, max_length=MAX_SPRINT_NAME_CHARS)
    parent: str | None = Field(None, max_length=20, description="the ref of the task it belongs to")
    assignees: list[str] = Field(default_factory=list, max_length=MAX_PEOPLE)
    roles: TasksRolesIn = Field(default_factory=TasksRolesIn)
    links: TasksLinksIn = Field(default_factory=TasksLinksIn)
    label: LabelIn | None = Field(None, description="a label above the project's default; an admin's to give")


class TasksItemEdit(BaseModel):
    """A change of a task: the fields given (null clears due_on, sprint and parent), the assignees when given (the
    whole list), the roles given, links to add and link ids to drop."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, min_length=1, max_length=MAX_TITLE_CHARS)
    description_md: str | None = Field(None, max_length=MAX_DESCRIPTION_CHARS)
    outcome_md: str | None = Field(None, max_length=MAX_DESCRIPTION_CHARS)
    type: str | None = Field(None, max_length=40)
    status: str | None = Field(None, max_length=40)
    priority: Literal[PRIORITIES] | None = None
    due_on: date | None = None
    sprint: str | None = Field(None, max_length=MAX_SPRINT_NAME_CHARS)
    parent: str | None = Field(None, max_length=20)
    assignees: list[str] | None = Field(None, max_length=MAX_PEOPLE)
    roles: TasksRolesIn = Field(default_factory=TasksRolesIn)
    links: TasksLinksIn = Field(default_factory=TasksLinksIn)
    unlink: list[int] = Field(default_factory=list, max_length=MAX_LINKS, description="ids of links to drop")

    def values(self) -> dict:
        """The fields set, with the null of a field that clears; a null elsewhere sets nothing."""
        clearable = {"due_on", "sprint", "parent"}
        fields = ("title", "description_md", "outcome_md", "type", "status", "priority", "due_on", "sprint", "parent")
        given = self.model_fields_set
        return {
            name: getattr(self, name)
            for name in fields
            if name in given and (getattr(self, name) is not None or name in clearable)
        }


class TasksBlockerIn(BaseModel):
    """A new blocker: why, and exactly one of the three things it waits on."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)
    waiting_member: str | None = Field(None, max_length=100, description="the login of a member with a grant")
    waiting_task: str | None = Field(None, max_length=20, description="the ref of another task of the project")
    waiting_text: str | None = Field(None, max_length=MAX_WAITING_CHARS, description="someone outside, in words")

    def waiting(self) -> Waiting:
        return Waiting(self.waiting_member, self.waiting_task, self.waiting_text)


class TasksBlockerEdit(BaseModel):
    """A change of a blocker: its reason, who it waits on (exactly one of the three, when given), or ``cleared``."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(None, min_length=1, max_length=MAX_REASON_CHARS)
    waiting_member: str | None = Field(None, max_length=100)
    waiting_task: str | None = Field(None, max_length=20)
    waiting_text: str | None = Field(None, max_length=MAX_WAITING_CHARS)
    cleared: bool = Field(False, description="true clears the blocker")

    def waiting(self) -> Waiting | None:
        given = Waiting(self.waiting_member, self.waiting_task, self.waiting_text)
        return given if given.given() else None


class TasksPerson(BaseModel):
    """A member of the project, whom a task may name."""

    login: str
    role: str = Field(description="the role of their grant on the project: reader, writer or admin")


class TasksSprint(BaseModel):
    name: str
    goal: str
    starts_on: date | None
    ends_on: date | None
    state: Literal[SPRINT_STATES]
    created_by: str
    created_at: datetime
    closed_at: datetime | None
    activated_at: datetime | None = Field(
        description="when it turned active; empty for a sprint that was active before the hub kept this"
    )
    tasks: int = Field(description="how many tasks it holds")


class TasksSprintIn(BaseModel):
    name: str = SprintName
    goal: str = Field("", max_length=MAX_GOAL_CHARS)
    starts_on: date | None = None
    ends_on: date | None = None
    state: Literal["planned", "active"] = "planned"


class TasksSprintEdit(BaseModel):
    """A change of a sprint; closing it moves its unfinished tasks to ``move_to``, or to no sprint."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=MAX_SPRINT_NAME_CHARS)
    goal: str | None = Field(None, max_length=MAX_GOAL_CHARS)
    starts_on: date | None = None
    ends_on: date | None = None
    state: Literal[SPRINT_STATES] | None = None
    move_to: str | None = Field(None, max_length=MAX_SPRINT_NAME_CHARS, description="closing: the sprint to move to")

    def values(self) -> dict:
        return {name: value for name, value in self.model_dump(exclude={"move_to"}).items() if value is not None}


class TasksCommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_COMMENT_CHARS)


class TasksImport(BaseModel):
    data: TasksFile = Field(description="the file, in the format evo-tasks/1")
    dry_run: bool = Field(False, description="read the file against the project and import nothing")


class TasksImportProblem(BaseModel):
    level: Literal["error", "warning"] = Field(
        description="an error stops the import; a warning says what it leaves out"
    )
    task: str | None = Field(description="the key of the file's task it is about")
    message: str


class TasksImportSkip(BaseModel):
    key: str
    task: str = Field(description="the ref of the task imported before from the same source")


class TasksImportResult(BaseModel):
    dry_run: bool
    tasks: int = Field(description="how many tasks it makes, or would make")
    sprints: int = Field(description="how many sprints it makes, or would make")
    links: int = Field(description="how many links it makes, or would make")
    created: list[str] = Field(description="the refs of the tasks made; none for a dry run")
    skipped: list[TasksImportSkip]
    problems: list[TasksImportProblem]


def settings_model(config: Settings, srs: bool) -> TasksSettings:
    statuses = [TasksStatus(key=s.key, name=s.name, group=s.group) for s in config.statuses]
    return TasksSettings(
        prefix=config.prefix, statuses=statuses, types=list(config.types), on_steps_done=config.on_steps_done, srs=srs
    )


def blocker_model(row: BlockerRow) -> TasksBlocker:
    return TasksBlocker(**row.__dict__)


def item_model(
    config: Settings, row: TaskRow, held: People, progress: tuple[int, int], codes: list[str], blocked: bool
) -> TasksItem:
    return TasksItem(
        number=row.number,
        ref=config.ref(row.number),
        type=row.type,
        title=row.title,
        status=row.status,
        status_group=config.group(row.status),
        priority=row.priority,
        due_on=row.due_on,
        sprint=row.sprint,
        parent=config.ref(row.parent_number) if row.parent_number else None,
        assignees=held.assignees,
        roles=TasksRoles(**held.roles),
        steps_done=progress[0],
        steps_total=progress[1],
        codes=codes,
        label=row.label,
        blocked=blocked,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def detail_model(
    item: TasksItem,
    row: TaskRow,
    shown: list[LinkView],
    blockers: list[BlockerRow],
    history: list[EventRow],
    said: list[CommentRow],
) -> TasksItemDetail:
    return TasksItemDetail(
        **item.model_dump(),
        description_md=row.description_md,
        outcome_md=row.outcome_md,
        external_ref=row.external_ref,
        links=[TasksLink(**view.__dict__) for view in shown],
        blockers=[blocker_model(blocker) for blocker in blockers],
        events=[TasksEvent(**event.__dict__) for event in history],
        comments=[comment_model(row) for row in said],
    )


def comment_model(row: CommentRow) -> TasksComment:
    return TasksComment(**row.__dict__)


def sprint_model(row: SprintRow) -> TasksSprint:
    return TasksSprint(**{name: value for name, value in row.__dict__.items() if name != "id"})


def import_model(config: Settings, found: Plan, dry_run: bool, made: list[int]) -> TasksImportResult:
    return TasksImportResult(
        dry_run=dry_run,
        tasks=len(found.tasks),
        sprints=len(found.sprints),
        links=found.links(),
        created=[config.ref(number) for number in made],
        skipped=[TasksImportSkip(key=key, task=config.ref(number)) for key, number in found.skipped],
        problems=[TasksImportProblem(**problem.as_json()) for problem in found.problems],
    )


__all__ = [
    "TasksBlocker",
    "TasksBlockerEdit",
    "TasksBlockerIn",
    "TasksComment",
    "TasksCommentIn",
    "TasksEvent",
    "TasksImport",
    "TasksImportProblem",
    "TasksImportResult",
    "TasksImportSkip",
    "TasksItem",
    "TasksItemDetail",
    "TasksItemEdit",
    "TasksItemIn",
    "TasksLink",
    "TasksLinksIn",
    "TasksPerson",
    "TasksRoles",
    "TasksRolesIn",
    "TasksRun",
    "TasksSettings",
    "TasksSprint",
    "TasksSprintEdit",
    "TasksSprintIn",
    "TasksStatus",
    "blocker_model",
    "comment_model",
    "detail_model",
    "import_model",
    "item_model",
    "settings_model",
    "sprint_model",
]
