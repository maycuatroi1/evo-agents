"""The tasks of an approved version (criterion a8 of plan srs-conflicts-and-tasks).

When a version of a document is approved and the project enables the plugin tasks, ``approved`` compares it with the
version approved before it (none the first time) and keeps the project's tasks in step with its use cases:

- a use case new in the version gets one frame task, titled with its code and title and linked to it: of type story
  when the project has that type (its first type otherwise), in the first status, carrying the document's label, made
  by the approver. Its external_key (``tables_srs.frame_task_key``: the document, the version, the use case's code) is
  how a second call finds it, so a call again or a replay makes no second one;
- a use case that changed, itself or a business rule under it (added, changed, moved or dropped), gets a comment of
  the approver on each open task linked to it (one whose status is not of group done or cancelled), with the link of
  the comparison of the two versions;
- a use case the version dropped moves each open task linked to it to the first status of group cancelled; the
  task's history names the hub, with the reason. A project whose statuses have no such group keeps the task, which
  gets a comment saying so.

A frame task made and a task cancelled each emit ``tasks.status`` (cause ``srs_approval``, the document and version
its source; ``evo_agents.hub.task_status``), in the same transaction.

Then the hub hands @ba a task run (``task_dispatch.hand_own_task``) that splits each open frame task of the document
with no child task yet into child tasks (``split_task``, the skill srs-tasks of @ba's pack), which it makes with
``tasks__create``, the frame task their parent, linked to the use case: that tool acts, for this run alone, for the
approver, whom srs_approval_tasks keeps for the run (``approved_by``, ``split_run_id``). When the agent type ba is not
on, is past its cap, or no frame task waits, there is no run and the outcome says why.

Everything goes through ``approved``, which takes (the document, the version approved, the version approved before,
the approver): the data the event ``srs.version.approved`` carries (plan loop-engine, criterion a3). The approval
emits that event (``events.version_approved``) and no longer calls ``approved``: the plugin's events contribution
``approvals`` (``events.on_version_approved``) calls it with the event's payload when the hub delivers it, in the
delivery's transaction, in every project that enables the plugin tasks, whether its Loop is on or off. It is
idempotent by (document, version approved): srs_approval_tasks holds one row a version, claimed first, so a second
call, an event delivered twice included, makes no task, comment or run.

The plugin srs imports no other core plugin: it reads the config of the plugin tasks with the hub's config layers and
writes the rows of its tasks through the hub's tables (``tables.tasks``), as the plugin tasks reads srs's items.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, task_status
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.hub.server.srs import BA, store
from evo_agents.hub.server.srs.diff import diff
from evo_agents.hub.server.srs.doc import Document, Item
from evo_agents.hub.server.srs.store import PLUGIN, DocumentRow, Snapshot
from evo_agents.hub.tables_srs import FRAME_TASK_PREFIX, frame_task_key
from evo_agents.hub.tables_tasks import MAX_CAUSE_CHARS, MAX_TITLE_CHARS, MAX_VALUE_CHARS
from evo_agents.plugins import config as plugin_config

log = logging.getLogger(__name__)

TASKS = "tasks"  # the plugin whose tasks an approval keeps (evo_agents.hub.server.tasks)
USE_CASE, BUSINESS_RULE = "use_case", "business_rule"
CANCELLED = "cancelled"  # the group of the status a task of a dropped use case moves to
FINISHED_GROUPS = ("done", CANCELLED)  # evo_agents.hub.server.tasks.FINISHED: a task nobody works on any more
FRAME_TYPE = "story"  # a frame task's type, when the project has it
MAX_FRAMES = 50  # the frame tasks one split run names
APPROVED_STATES = ("approved", "superseded")  # of a version that was approved, now or before a newer one


@dataclass(frozen=True)
class TasksConfig:
    """What an approval reads of the config of the plugin tasks in the project."""

    prefix: str
    first: str  # the status a new task takes
    cancelled: str | None  # the first status of group cancelled
    finished: tuple[str, ...]  # the statuses of groups done and cancelled
    type: str  # a frame task's
    groups: tuple[tuple[str, str], ...] = ()  # (status, its group) of each status

    def ref(self, number: int) -> str:
        return f"{self.prefix}-{number}"

    def group(self, status: str | None) -> str | None:
        return next((group for key, group in self.groups if key == status), None)


@dataclass(frozen=True)
class UseCases:
    """What became of the use cases from one approved version to the next."""

    added: list[Item]
    changed: list[Item]
    dropped: list[Item]


@dataclass
class Outcome:
    """What one approval did: the numbers of the frame tasks made, of the tasks commented on and of the tasks
    cancelled, the split run handed or why there is none; ``again`` when the version was handled before, by a call
    whose outcome this is, and this call did nothing."""

    created: list[int] = field(default_factory=list)
    commented: list[int] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    split_run_id: int | None = None
    no_split: str | None = None
    again: bool = False

    def as_json(self) -> dict:
        return {
            "created": self.created,
            "commented": self.commented,
            "cancelled": self.cancelled,
            "no_split": self.no_split,
        }


@dataclass(frozen=True)
class _Approval:
    """The work of one approval: the document and its project's name, the versions compared, the approver, the
    config of tasks."""

    doc: DocumentRow
    project_id: int
    project: str
    version: Snapshot
    previous: Snapshot | None
    approver_id: int
    approver: str
    config: TasksConfig

    @property
    def number(self) -> int:
        return self.version.version.number

    def because(self, what: str) -> str:
        return f"{what} in version {self.number} of {self.doc.slug}, which {self.approver} approved"

    async def announce(self, conn: AsyncConnection, task_id: int, old: str | None, new: str) -> None:
        """Emit ``tasks.status`` of task ``task_id``, which this approval made (``old`` None) or cancelled: the hub
        changed it, the document and the version its source."""
        source = {"document": self.doc.slug, "document_id": self.doc.id, "version": self.number}
        groups = self.config.group(old), self.config.group(new)
        change = task_status.StatusChange(
            task_id, old, new, *groups, task_status.SRS_APPROVAL, self.config.prefix, source=source
        )
        await task_status.emit(conn, self.project_id, change)


# The config of tasks


async def tasks_config(conn: AsyncConnection, plugin, project_id: int) -> TasksConfig | None:
    """The config of the plugin tasks (its row of ``plugin_calls.enabled_query``) in project ``project_id``, read as
    the plugin reads it; None when it names no status or no type."""
    stacks = await stored_stacks(conn, [plugin.id], project_id, None)
    merged = plugin_config.merge(plugin.manifest.get("config"), stacks.get(plugin.id, []))
    values = {name: item.value for name, item in merged.fields.items() if not item.secret}
    statuses = [status for status in values.get("statuses") or [] if isinstance(status, dict) and status.get("key")]
    types = [name for name in values.get("types") or [] if isinstance(name, str)]
    if not statuses or not types:
        return None
    cancelled = next((status["key"] for status in statuses if status.get("group") == CANCELLED), None)
    finished = tuple(status["key"] for status in statuses if status.get("group") in FINISHED_GROUPS)
    kind = FRAME_TYPE if FRAME_TYPE in types else types[0]
    groups = tuple((status["key"], status.get("group")) for status in statuses)
    return TasksConfig(values.get("prefix") or "T", statuses[0]["key"], cancelled, finished, kind, groups)


# What changed


def _use_case_above(doc: Document, at: int) -> str | None:
    """The key of the use case item ``at`` of ``doc`` sits under, or None."""
    return next((doc.items[up].key for up in doc.ancestors(at) if doc.items[up].kind == USE_CASE), None)


def use_case_changes(old: Document | None, new: Document) -> UseCases:
    """The use cases of ``new`` that ``old`` lacks (every one when ``old`` is None), those both hold that changed,
    themselves or by a business rule under them added, changed, moved or dropped, and those of ``old`` that ``new``
    dropped; items matched by key."""
    before = {item.key: at for at, item in enumerate(old.items)} if old is not None else {}
    now = {item.key: at for at, item in enumerate(new.items)}
    added = [item for item in new.items if item.kind == USE_CASE and item.key not in before]
    if old is None:
        return UseCases(added, [], [])
    dropped = [item for item in old.items if item.kind == USE_CASE and item.key not in now]
    touched: list[str | None] = []
    for change in diff(old, new):
        if change.kind == USE_CASE and change.change == "changed":
            touched.append(change.key)
        elif change.kind == BUSINESS_RULE:
            if change.key in now:
                touched.append(_use_case_above(new, now[change.key]))
            if change.key in before:
                touched.append(_use_case_above(old, before[change.key]))
    changed = [new.items[now[key]] for key in dict.fromkeys(touched) if key in now and key in before]
    return UseCases(added, changed, dropped)


# Writing tasks


async def _history(conn: AsyncConnection, task_id: int, name: str, old: str | None, new: str | None, cause: str):
    """A row of the hub (no actor) in the history of task ``task_id``."""
    values = {
        "task_id": task_id,
        "actor_id": None,
        "field": name,
        "from_value": old[:MAX_VALUE_CHARS] if old else old,
        "to_value": new[:MAX_VALUE_CHARS] if new else new,
        "cause": cause[:MAX_CAUSE_CHARS],
    }
    await conn.execute(insert(tables.tasks.events).values(**values))


def _frame_text(work: _Approval, item: Item, code: str) -> str:
    page = store.item_path(work.project, work.doc.slug, code)
    return (
        f'The frame task of use case {code} ("{item.title}") of the SRS document {work.doc.slug}, new in version '
        f"{work.number}, which {work.approver} approved: {page}\n\n"
        f"@{BA} splits it into child tasks, each linked to the use case; the work is done and tracked in them."
    )


async def _frame(conn: AsyncConnection, work: _Approval, item: Item, outcome: Outcome) -> None:
    """The frame task of use case ``item``, new in the version, unless a call made it before."""
    t, k = tables.tasks.items, tables.tasks.links
    project_id, code = work.project_id, item.code or item.key
    key = frame_task_key(work.doc.slug, work.number, code)
    if await conn.scalar(select(t.c.id).where(t.c.project_id == project_id, t.c.external_key == key)) is not None:
        return
    number = (await conn.scalar(select(func.max(t.c.number)).where(t.c.project_id == project_id)) or 0) + 1
    values = {
        "project_id": project_id,
        "number": number,
        "type": work.config.type,
        "title": f"{code}: {item.title}"[:MAX_TITLE_CHARS],
        "description_md": _frame_text(work, item, code),
        "status": work.config.first,
        "label": work.doc.label,
        "external_key": key,
        "external_ref": {"source": PLUGIN, "document": work.doc.slug, "version": work.number, "use_case": code},
        "created_by": work.approver_id,
    }
    task_id = await conn.scalar(insert(t).values(**values).returning(t.c.id))
    cause = work.because(f"use case {code} is new")
    await _history(conn, task_id, "created", None, work.config.ref(number), cause)
    link = {"task_id": task_id, "kind": "srs_item", "srs_item_id": work.version.ids[item.key]}
    await conn.execute(insert(k).values(**link, created_by=work.approver_id))
    await _history(conn, task_id, "link", None, f"srs_item {code}", cause)
    await work.announce(conn, task_id, None, work.config.first)
    outcome.created.append(number)


async def _open_tasks(conn: AsyncConnection, work: _Approval, item_id: int) -> list:
    """The open tasks of the project linked to SRS item ``item_id``, by number."""
    t, k = tables.tasks.items, tables.tasks.links
    query = (
        select(t.c.id, t.c.number, t.c.status)
        .join_from(k, t, t.c.id == k.c.task_id)
        .where(k.c.kind == "srs_item", k.c.srs_item_id == item_id, t.c.project_id == work.project_id)
        .where(t.c.status.not_in(work.config.finished))
        .distinct()
        .order_by(t.c.number)
    )
    return list(await conn.execute(query))


async def _comment(conn: AsyncConnection, work: _Approval, task_id: int, body: str) -> None:
    values = {"task_id": task_id, "author_id": work.approver_id, "body": body}
    await conn.execute(insert(tables.tasks.comments).values(**values))


async def _changed(conn: AsyncConnection, work: _Approval, item: Item, outcome: Outcome) -> None:
    """A comment of the approver on each open task linked to use case ``item``, which changed."""
    code, low = item.code or item.key, work.previous.version.number
    compare = store.compare_path(work.project, work.doc.slug, low, work.number)
    body = (
        f"{work.because(f'Use case {code} changed')}: see what changed since version {low} at {compare}, and "
        "check that this task still matches it."
    )
    for task in await _open_tasks(conn, work, work.version.ids[item.key]):
        await _comment(conn, work, task.id, body)
        outcome.commented.append(task.number)


async def _dropped(conn: AsyncConnection, work: _Approval, item: Item, outcome: Outcome) -> None:
    """Each open task linked to use case ``item``, which the version dropped, to the first status of group
    cancelled, its history naming the hub and why; a comment when the project has no such status."""
    t, code = tables.tasks.items, item.code or item.key
    cause = work.because(f"use case {code} was dropped")
    for task in await _open_tasks(conn, work, work.previous.ids[item.key]):
        if work.config.cancelled is None:
            body = (
                f"{cause[0].upper()}{cause[1:]}. The project has no status of group cancelled: the task keeps its own."
            )
            await _comment(conn, work, task.id, body)
            outcome.commented.append(task.number)
            continue
        await conn.execute(
            update(t).where(t.c.id == task.id).values(status=work.config.cancelled, updated_at=func.now())
        )
        await _history(conn, task.id, "status", task.status, work.config.cancelled, cause)
        await work.announce(conn, task.id, task.status, work.config.cancelled)
        outcome.cancelled.append(task.number)


# The split run


async def _frames_to_split(conn: AsyncConnection, work: _Approval) -> dict[int, tuple[str, list[str]]]:
    """number -> (title, the codes of the SRS items it links) of each open frame task of the document with no child
    task, the first MAX_FRAMES by number."""
    t, k, i = tables.tasks.items, tables.tasks.links, tables.srs.items
    child = t.alias("child")
    prefix = f"{FRAME_TASK_PREFIX}{work.doc.slug}:"
    query = (
        select(t.c.number, t.c.title, i.c.code)
        .select_from(
            t.outerjoin(k, (k.c.task_id == t.c.id) & (k.c.kind == "srs_item")).outerjoin(i, i.c.id == k.c.srs_item_id)
        )
        .where(t.c.project_id == work.project_id, t.c.external_key.startswith(prefix, autoescape=True))
        .where(t.c.status.not_in(work.config.finished), ~exists().where(child.c.parent_id == t.c.id))
        .order_by(t.c.number, k.c.id)
    )
    found: dict[int, tuple[str, list[str]]] = {}
    for row in await conn.execute(query):
        title, codes = found.setdefault(row.number, (row.title, []))
        if row.code and row.code not in codes:
            codes.append(row.code)
    return dict(list(found.items())[:MAX_FRAMES])


def split_task(
    project: str,
    slug: str,
    number: int,
    approver: str,
    frames: dict[int, tuple[str, list[str]]],
    ref: Callable[[int], str],
) -> str:
    """The task of @ba's run that splits ``frames`` (number -> (title, codes)) of document ``slug`` of ``project``,
    whose version ``number`` ``approver`` approved; ``ref`` shows a task's number."""
    listed = [
        f"- {ref(n)}: {title} (use case {', '.join(codes) or 'not linked'})" for n, (title, codes) in frames.items()
    ]
    first = ref(next(iter(frames)))
    return "\n".join(
        [
            f"Split the frame tasks of the SRS document {slug} of project {project} into child tasks. Version "
            f"{number} of it was approved by {approver}, and the hub made a frame task for each use case new in it.",
            "",
            "The open frame tasks of the document that have no child task yet:",
            *listed,
            "",
            "Follow the skill srs-tasks of your pack, for each frame task:",
            f"1. Read its use case with srs__read (document {slug}, item: the use case's code): its actor, its steps, "
            "the business rules under it and the screens it uses.",
            "2. Split it into child tasks, each one piece of work that one developer or tester finishes and checks on "
            "their own (a screen, an API, the check of a business rule, the tests of the flow): a handful, not one a "
            "step. Every business rule of the use case is in one child task.",
            "3. Create each with tasks__create: a title of its own, a description_md that says what to build and how "
            f"to check it is done (the steps and business rules it covers), parent: the frame task's number (such as "
            f"{first}), and links: the use case's code. The tool makes the task for {approver}, who approved the "
            "version, and refuses a child whose title another child of the same frame task has: that child exists "
            "already, so go on with the next.",
            "",
            "You create child tasks only. You change no frame task, no SRS document and no finding, you name no "
            "assignee, and you change no file of the repositories of this run.",
        ]
    )


async def _split(conn: AsyncConnection, work: _Approval) -> tuple[int | None, str | None]:
    """(the task run of @ba that splits the frame tasks of the document, None) or (None, why there is none)."""
    # Imported here: task_dispatch's own imports go round a cycle (agent_packs, runs, claims) when it is the first
    # module of the server a process loads, which a job that imports this module first would make it.
    from evo_agents.hub.server.task_dispatch import hand_own_task

    frames = await _frames_to_split(conn, work)
    if not frames:
        return None, f"no open frame task of {work.doc.slug} waits to be split"
    task = split_task(work.project, work.doc.slug, work.number, work.approver, frames, work.config.ref)
    title = f"@{BA} splits the tasks of {work.doc.slug} v{work.number}"
    return await hand_own_task(conn, PLUGIN, work.project_id, BA, task, title)


# The approval


async def _done_before(conn: AsyncConnection, version_id: int) -> Outcome:
    a = tables.srs.approval_tasks
    row = (await conn.execute(select(a.c.split_run_id, a.c.outcome).where(a.c.version_id == version_id))).one()
    kept = row.outcome or {}
    return Outcome(
        created=list(kept.get("created") or []),
        commented=list(kept.get("commented") or []),
        cancelled=list(kept.get("cancelled") or []),
        split_run_id=row.split_run_id,
        no_split=kept.get("no_split"),
        again=True,
    )


async def _names(conn: AsyncConnection, project_id: int, user_id: int) -> tuple[str, str]:
    """(the project's name, the member's login)."""
    p, u = tables.projects, tables.users
    project = await conn.scalar(select(p.c.name).where(p.c.id == project_id))
    return project, await conn.scalar(select(u.c.login).where(u.c.id == user_id))


async def approved(
    conn: AsyncConnection, document_id: int, version: int, previous: int | None, approver_id: int
) -> Outcome | None:
    """Keep the project's tasks in step with version ``version`` of document ``document_id``, which member
    ``approver_id`` approved after version ``previous`` (None: no version was approved before), in the caller's
    transaction (see the module). What was done; None when the project does not enable the plugin tasks or the
    version was never approved. A second call for the same version does nothing and answers the first's outcome,
    ``again``."""
    d = tables.srs.documents
    project_id = await conn.scalar(select(d.c.project_id).where(d.c.id == document_id))
    plugin = await enabled_in(conn, TASKS, project_id) if project_id is not None else None
    doc = await store.document_by_id(conn, project_id, document_id) if plugin is not None else None
    row = await store.version(conn, doc, version) if doc is not None else None
    if row is None or row.state not in APPROVED_STATES:
        return None
    a = tables.srs.approval_tasks
    claim = pg_insert(a).values(version_id=row.id, previous_version=previous, approved_by=approver_id)
    claimed = await conn.scalar(claim.on_conflict_do_nothing(index_elements=[a.c.version_id]).returning(a.c.id))
    if claimed is None:
        return await _done_before(conn, row.id)
    outcome = Outcome()
    config = await tasks_config(conn, plugin, project_id)
    if config is None:
        outcome.no_split = f"the config of plugin {TASKS} names no status or no type: no task was made"
    else:
        project, approver = await _names(conn, project_id, approver_id)
        before = await store.snapshot(conn, await store.version(conn, doc, previous)) if previous else None
        now = await store.snapshot(conn, row)
        work = _Approval(doc, project_id, project, now, before, approver_id, approver, config)
        changes = use_case_changes(before.doc if before else None, work.version.doc)
        for item in changes.added:
            await _frame(conn, work, item, outcome)
        for item in changes.changed:
            await _changed(conn, work, item, outcome)
        for item in changes.dropped:
            await _dropped(conn, work, item, outcome)
        outcome.split_run_id, outcome.no_split = await _split(conn, work)
    values = {"split_run_id": outcome.split_run_id, "outcome": outcome.as_json()}
    await conn.execute(update(a).where(a.c.id == claimed).values(**values))
    extra = {"document_id": document_id, "version": version, "run_id": outcome.split_run_id, "why": outcome.no_split}
    extra |= {"frames": outcome.created, "commented": outcome.commented, "cancelled": outcome.cancelled}
    log.info("srs approval kept the tasks", extra=extra)
    return outcome


__all__ = [
    "MAX_FRAMES",
    "Outcome",
    "TasksConfig",
    "UseCases",
    "approved",
    "split_task",
    "tasks_config",
    "use_case_changes",
]
