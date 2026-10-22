"""The engine ``rules/1``: the table of rules from a delta to a work item of the report's section Loop (plan loop-engine,
criterion a4), as three pure functions over a ``View`` (``model.Engine``). Nothing here reads a table or writes.

What an event makes (``rules``):

- ``canvas.baseline`` moving a claim to testing (row 3): a ``use_case`` work item for the claim, when it serves an
  active goal with a metric (``View.goal_of_canvas``); it waits in watching (reason ``stable``) until the baseline has
  been approved ``loop.stable_hours``. Key ``use_case:<canvas>:<claim>:b<baseline>``.
- ``canvas.baseline`` moving a claim to validated, invalidated or retired (rows 4 and 5): nothing new; the result is
  noted once in the claim's chain, and invalidated or retired comments on each open frame task of the chain. The work
  items the claim's end leaves stale are cancelled by ``reconcile``, which reads the claim's status.
- ``srs.version.approved`` of a version a ``use_case`` work item's run drafted, or adding or changing a use case a
  claim with a chain links (row 6): that ``use_case`` work item done, and for each use case the version added, a
  ``build`` work item waiting (reason ``auto_tasks``) for the frame task the plugin srs makes of it, when the project
  enables the plugin tasks. The Loop makes no task. Key ``build:<external key of the frame task>``
  (``tables_srs.frame_task_key``), known before the task exists.
- ``tasks.status``, ``canvas.claim``, ``signal.posted`` and any other version approved: nothing (rows 8, 12); a task
  closed is read by ``reconcile``, and the value check of the canvas runs on signals by itself.

What the clock makes (``scheduled``, rows 1 and 2): a ``brainstorm`` for each active goal with a metric whose target is
not met (no signal in the window counts as not met) and that has no brainstorm unfinished: first for its newest
revision (key ``brainstorm:<goal>:r<revision>``), else for this week once its time came, when no brainstorm of the goal
was made since and no proposal of a claim a brainstorm of the goal made is still open (key
``brainstorm:<goal>:<ISO week>``).

Where a work item moves (``reconcile``): cancelled when its goal is paused or closed (row 14), its claim invalidated
or retired (row 4), or its frame task closed before a run was handed out for it (row 8); a ``use_case`` waiting out
``loop.stable_hours`` to queued; a ``build`` from its frame task's approval to the task, then to queued once the task
is ready (group todo, the split run ended, in an active sprint when ``loop.require_sprint``; row 7); a ``build``
whose author run wrote a plan and ended to done, with a ``deliver`` of that plan (row 9); a ``deliver`` whose merge
requests are all through to done (row 10; one a person merged on GitLab, which the queue closed, is through), one
reverted a failure (row 11), and one whose repos the queue does not merge to a person (``merge_manual``) until the
plan marks them merged. A run a person cancelled cancels its work item.
Test cards past their deadline make nothing (row 13): the canvas's baseline asks about them.

A work item waits for a person (``waiting_human``), and fails nothing, while its run's agent waits for its owner (an
author's question, a plan run's decision), while the version a use_case drafted waits for its review or for the settle
chat of an open finding of level decide, and while a merge request of its plan is held. A failure (a run failed or lost
for good, a run that ended without what it was for, a revert) queues it again the first time, puts it in triage the
second and asks a person from the third (``_failure``; the tick's ``stall`` opens the thread and asks the decision);
changes asked of a use_case's version queue it again with no failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from evo_agents.hub.hub_events import BASELINE, SRS_APPROVED
from evo_agents.hub.server.loop.model import (
    ACTIVE,
    ASK_AT,
    BA,
    BRAINSTORM,
    BUILD,
    CANCELLED,
    CANCELLED_EVENT,
    DELIVER,
    DONE,
    DONE_EVENT,
    DRAFT,
    ENDED_CLAIMS,
    ENGINE,
    FAILED,
    IN_REVIEW,
    MERGE_FINAL,
    MERGE_HELD,
    MERGED,
    PAUSED,
    QUEUED,
    READY,
    REVERTED,
    RUNNING,
    STRATEGIST,
    TESTING,
    TODO,
    TRIAGE_AT,
    TRIAGE_WAIT_MINUTES,
    USE_CASE,
    VALIDATED,
    WAITING,
    WAITING_HUMAN,
    WATCHING,
    WITHDRAWN,
    Effect,
    Event,
    ItemView,
    MergeView,
    Note,
    RunView,
    Transition,
    VersionView,
    View,
    WorkSpec,
)
from evo_agents.hub.tables_srs import code_key, frame_task_key

NOTED_RESULTS = (VALIDATED, *ENDED_CLAIMS)  # what a baseline writes into a claim's chain
ENDED_COMMENT = "Claim {claim} of canvas {canvas} is {status} since baseline {baseline}: the Loop stops its work items."


# What an event makes


def _use_case(event: Event, view: View, moved: dict) -> list[Effect]:
    """Row 3: a claim moved to testing by a baseline."""
    payload = event.payload
    goal = view.goal_of_canvas(payload["canvas_id"])
    if goal is None:
        return []
    hours = float(view.policy.get("stable_hours") or 0)
    stable_at = event.created_at + timedelta(hours=hours)
    waits = view.now < stable_at
    detail = {"baseline": payload["baseline"], "from": moved.get("from")}
    if waits:
        detail["stable_at"] = stable_at.isoformat()
    return [
        WorkSpec(
            USE_CASE,
            f"use_case:{payload['canvas']}:{moved['claim']}:b{payload['baseline']}",
            goal.id,
            state=WATCHING if waits else QUEUED,
            reason="stable" if waits else None,
            claim_id=moved["claim_id"],
            owner=BA,
            detail=detail,
        )
    ]


def _noted(event: Event, view: View, moved: dict) -> list[Effect]:
    """Rows 4 and 5: a claim validated, invalidated or retired by a baseline, noted in its chain."""
    payload, to = event.payload, moved["to"]
    chain = view.chain(moved["claim_id"])
    if not chain:
        return []
    detail = {"status": to, "canvas": payload["canvas"], "claim": moved["claim"], "baseline": payload["baseline"]}
    key = f"noted:{payload['canvas']}:{moved['claim']}:b{payload['baseline']}"
    if to not in ENDED_CLAIMS:
        return [Note(moved["claim_id"], key, detail)]
    tasks = sorted({item.task_id for item in chain if item.task_id is not None})
    open_tasks = tuple(task for task in tasks if task in view.tasks and view.tasks[task].open)
    goal = view.goals.get(chain[-1].goal_id) if chain[-1].goal_id is not None else None
    if not open_tasks or goal is None:
        return [Note(moved["claim_id"], key, detail)]
    comment = ENDED_COMMENT.format(
        claim=moved["claim"], canvas=payload["canvas"], status=to, baseline=detail["baseline"]
    )
    return [Note(moved["claim_id"], key, detail, comment=comment, comment_on=open_tasks, author_id=goal.owner_id)]


def _baseline(event: Event, view: View) -> list[Effect]:
    effects: list[Effect] = []
    for moved in event.payload.get("transitions") or []:
        if moved.get("to") == TESTING:
            effects.extend(_use_case(event, view, moved))
        elif moved.get("to") in NOTED_RESULTS:
            effects.extend(_noted(event, view, moved))
    return effects


def _codes(payload: dict, change: str) -> list[str]:
    return list(((payload.get("items") or {}).get(change) or {}).get(USE_CASE) or [])


def _chains(event: Event, view: View) -> list[tuple[ItemView, list[str]]]:
    """(the use_case work item, the use cases the version added for it) of each chain the approved version answers."""
    payload = event.payload
    added, changed = _codes(payload, "added"), _codes(payload, "changed")
    drafted = view.versions.get(payload["version_id"])
    found: dict[int, tuple[ItemView, list[str]]] = {}
    for item in view.items:
        if item.kind != USE_CASE:
            continue
        follows = view.drafts.get(item.id)  # a later version of the document its run drafted (a settle chat's)
        if (drafted is not None and drafted in item.runs) or (
            not item.finished and follows is not None and follows.document_id == payload.get("document_id")
        ):
            found[item.id] = (item, added)
    touched = {code_key(code) for code in (*added, *changed)}
    for claim in view.claims.values():
        links = {code_key(code) for code in claim.use_cases}
        cases = [item for item in view.chain(claim.id) if item.kind == USE_CASE]
        if not cases or not links & touched:
            continue
        item = next((case for case in reversed(cases) if not case.finished), cases[-1])
        if item.id not in found:
            found[item.id] = (item, [code for code in added if code_key(code) in links])
    return [found[key] for key in sorted(found)]


def _approved(event: Event, view: View) -> list[Effect]:
    """Row 6: a version answering a use_case work item, approved."""
    payload, effects = event.payload, []
    for item, codes in _chains(event, view):
        if not item.finished:
            detail = {"document": payload["document"], "version": payload["version"]}
            effects.append(Transition(item.id, DONE, DONE_EVENT, detail=detail))
        goal = view.goals.get(item.goal_id) if item.goal_id is not None else None
        if not view.tasks_on or goal is None or goal.status != ACTIVE:
            continue
        for code in codes:
            frame = frame_task_key(payload["document"], payload["version"], code)
            effects.append(
                WorkSpec(
                    BUILD,
                    f"build:{frame}",
                    goal.id,
                    state=WATCHING,
                    reason="auto_tasks",
                    claim_id=item.claim_id,
                    srs_document_id=payload["document_id"],
                    use_case=code,
                    srs_version=payload["version"],
                    parent_id=item.id,
                    detail={"frame": frame, "version_id": payload["version_id"]},
                )
            )
    return effects


# What the clock makes


def _brainstorm(view: View, goal) -> WorkSpec | None:
    revision = f"brainstorm:{goal.code}:r{goal.revision}"
    if revision not in view.keys:
        return WorkSpec(BRAINSTORM, revision, goal.id, owner=STRATEGIST, detail={"revision": goal.revision})
    if view.week is None or view.week_at is None or view.proposals.get(goal.id, 0):
        return None
    weekly, last = f"brainstorm:{goal.code}:{view.week}", view.brainstormed.get(goal.id)
    if weekly in view.keys or (last is not None and last >= view.week_at):
        return None  # this week's look came, or a brainstorm of the goal was made since it came
    return WorkSpec(BRAINSTORM, weekly, goal.id, owner=STRATEGIST, detail={"week": view.week})


# Where a work item moves


def _cancel(item: ItemView, reason: str, **detail) -> Transition:
    return Transition(item.id, CANCELLED, CANCELLED_EVENT, reason=reason, detail=detail)


def _stale(item: ItemView, view: View) -> Transition | None:
    """Rows 4, 8 and 14: the work item's goal paused or closed, its claim ended, or its frame task closed before a
    run was handed out for it."""
    goal = view.goals.get(item.goal_id) if item.goal_id is not None else None
    if goal is None or goal.status != ACTIVE:
        return _cancel(item, "goal_paused" if goal is not None and goal.status == PAUSED else "goal_closed")
    claim = view.claims.get(item.claim_id) if item.claim_id is not None else None
    if claim is not None and claim.status in ENDED_CLAIMS:
        return _cancel(item, f"claim_{claim.status}")
    if item.kind in (BUILD, DELIVER) and item.task_id is not None and item.state in (QUEUED, WATCHING):
        task = view.tasks.get(item.task_id)
        if task is None or not task.open:
            return _cancel(item, "task_closed", group=None if task is None else task.group)
    return None


def _run_of(item: ItemView, view: View) -> RunView | None:
    return view.runs.get(item.run_id) if item.run_id is not None else None


def _run_end(item: ItemView, view: View) -> Transition | None:
    """A run of the work item that ended other than done: cancelled by a person, or a failure."""
    run = _run_of(item, view)
    if run is None or not run.ended or run.state == "done":
        return None
    if run.state == "cancelled":
        return _cancel(item, "person", run_id=run.id)
    return _failure(item, run_id=run.id, run_state=run.state)


def _failure(item: ItemView, **detail) -> Transition:
    """A failure of the work item (the report's section Triage): queued again at the first, triage at the second
    (watching until the chat run of its triage thread ends, TRIAGE_WAIT_MINUTES at most), and a decision of a person
    from the third on (waiting_human, ``loop_decision``)."""
    count = item.failures + 1
    detail = {**detail, "failures": count}
    if count >= ASK_AT:
        return Transition(item.id, WAITING_HUMAN, FAILED, reason="loop_decision", detail=detail, failed=True)
    if count >= TRIAGE_AT:
        return Transition(item.id, WATCHING, FAILED, reason="triage", detail=detail, failed=True)
    return Transition(item.id, QUEUED, FAILED, detail=detail, failed=True)


def _triaged(item: ItemView, view: View) -> Transition | None:
    """A work item in triage queued again once the tick opened its thread and the thread's chat run ended, or
    TRIAGE_WAIT_MINUTES passed, or no agent was asked."""
    triage = item.detail.get("triage")
    if not isinstance(triage, dict) or (triage.get("failures"), triage.get("attempts")) != (
        item.failures,
        item.attempts,
    ):
        return None  # the tick has not opened this failure's thread yet
    chat = view.runs.get(triage["run_id"]) if isinstance(triage.get("run_id"), int) else None
    since = triage.get("at")
    waited = since is None or view.now >= datetime.fromisoformat(since) + timedelta(minutes=TRIAGE_WAIT_MINUTES)
    if chat is None or chat.ended or waited:
        return Transition(item.id, QUEUED, READY, detail={"after_triage": item.failures})
    return None


def _asks(item: ItemView, run: RunView, reason: str) -> Transition | None:
    """A work item whose run's agent waits for its owner (a question, a decision) waits for a person with
    ``reason``; back to running once the agent goes on."""
    if item.state == RUNNING and run.asks:
        return Transition(item.id, WAITING_HUMAN, WAITING, reason=reason, detail={"asked_in": run.id})
    if item.state == WAITING_HUMAN and item.reason == reason and not run.asks and not run.ended:
        return Transition(item.id, RUNNING, READY, detail={"asked_in": None})
    return None


def _brainstormed(item: ItemView, view: View) -> Transition | None:
    """A brainstorm whose task run is done, with the proposals it made."""
    run = _run_of(item, view)
    if item.state == RUNNING and run is not None and run.state == "done":
        return Transition(item.id, DONE, DONE_EVENT, detail={"run_id": run.id, "proposals": run.proposals})
    return None


def _in_review(item: ItemView, version: VersionView) -> Transition | None:
    """Where the version a use_case drafted leaves it: waiting for its review (``srs_review``), for the settle chat of
    an open finding of level decide (``srs_finding``), queued again with the reviewer's request for changes, or
    cancelled when a person withdrew it. None of these is a failure; approved is row 6's."""
    where = {"document": version.document, "version": version.number}
    if version.state == WITHDRAWN:
        return _cancel(item, "person", **where)
    if version.state == DRAFT and version.changes:
        return Transition(item.id, QUEUED, READY, detail={"srs_changes": {**where, "version_id": version.id}})
    if version.state != IN_REVIEW:
        return None
    reason, srs = "srs_review", dict(where)
    if version.decide:
        reason, srs = "srs_finding", {**where, "findings": list(version.decide), "chat": version.chat}
    if item.state == WAITING_HUMAN and item.reason == reason and item.detail.get("srs") == srs:
        return None
    return Transition(item.id, WAITING_HUMAN, WAITING, reason=reason, detail={"srs": srs})


def _drafted(item: ItemView, view: View) -> Transition | None:
    """A use_case past its stable hours; one whose task run ended, by the version it drafted. A use_case is done when
    its version is approved (row 6), not when its run ends; a run done with no version in review, or none newer than
    the one changes were asked of, is a failure."""
    if item.state == WATCHING and item.reason == "stable":
        stable_at = item.detail.get("stable_at")
        if stable_at is None or view.now >= datetime.fromisoformat(stable_at):
            return Transition(item.id, QUEUED, READY)
        return None
    run = _run_of(item, view)
    if item.state not in (RUNNING, WAITING_HUMAN) or (run is not None and not run.ended):
        return None
    version = view.drafts.get(item.id)
    asked = item.detail.get("srs_changes")
    again = isinstance(asked, dict) and version is not None and asked.get("version_id") == version.id
    if item.state == RUNNING and (version is None or (again and version.changes)):
        if run is not None and run.state == "done":
            return _failure(item, run_id=run.id, why="the task run sent no new version of an SRS document to review")
        return None
    return None if version is None else _in_review(item, version)


def _frame(item: ItemView, view: View) -> Transition | None:
    """Row 7: a build from the approval of its version to its frame task, and on to queued once the task is ready."""
    approval = view.approvals.get(item.detail.get("version_id"))
    if approval is None:
        return None  # the plugin srs has not handled the approval yet
    task = view.tasks.get(item.task_id) if item.task_id is not None else view.frames.get(item.detail.get("frame"))
    if task is None:
        return _cancel(item, "no_frame_task", frame=item.detail.get("frame"))
    sprint = not view.policy.get("require_sprint") or task.sprint_active
    if task.group == TODO and approval.split_ended and sprint:
        return Transition(item.id, QUEUED, READY, task_id=task.id, detail={"task": task.number})
    if item.task_id is None or item.reason != "split":
        return Transition(item.id, WATCHING, WAITING, reason="split", task_id=task.id, detail={"task": task.number})
    return None


def _build(item: ItemView, view: View) -> Transition | None:
    """Rows 7 and 9: a build to its frame task; its author run waiting for a person's answer; written and ended, a
    deliver of its plan."""
    if item.state == WATCHING:
        return _frame(item, view)
    run = _run_of(item, view) if item.state in (RUNNING, WAITING_HUMAN) else None
    if run is None:
        return None
    asking = _asks(item, run, "author_question")
    if asking is not None or run.state != "done":
        return asking
    if run.plan_id is None:
        return _failure(item, run_id=run.id, why="the author run ended without a plan")
    deliver = WorkSpec(
        DELIVER,
        f"deliver:{run.plan_id}",
        item.goal_id,
        claim_id=item.claim_id,
        srs_document_id=item.srs_document_id,
        use_case=item.use_case,
        srs_version=item.srs_version,
        task_id=item.task_id,
        plan_id=run.plan_id,
        parent_id=item.id,
    )
    return Transition(item.id, DONE, DONE_EVENT, plan_id=run.plan_id, detail={"plan": run.plan_id}, spawn=(deliver,))


def _merging(item: ItemView, view: View, run: RunView) -> Transition:
    """A deliver whose plan run is done: waiting on the merge queue, or on a person for the repos the queue does not
    merge (``merge_manual``)."""
    detail = {"run_id": run.id, "repos": list(run.repos)}
    if run.repos and all(repo in view.manual for repo in run.repos):
        return Transition(item.id, WAITING_HUMAN, WAITING, reason="merge_manual", detail=detail)
    return Transition(item.id, WATCHING, WAITING, reason="merge_queue", detail=detail)


def _released(item: ItemView, merges: tuple) -> Transition:
    shas = [merge.after_sha for merge in merges if merge.after_sha]
    merged = [merge.merged_at for merge in merges if merge.merged_at is not None]
    released = max(merged).isoformat() if merged else None
    return Transition(item.id, DONE, DONE_EVENT, detail={"after_sha": shas, "released_at": released})


def _through(merge: MergeView) -> bool:
    """Whether a merge request of the queue is through: what follows its merge is over, or it merged where its repo
    names no check job, or a person merged it on GitLab and the queue closed it (``outside``)."""
    return merge.state == MERGE_FINAL or (merge.state == MERGED and not merge.checked) or merge.outside


def _merged(item: ItemView, view: View) -> Transition | None:
    """Rows 10 and 11: each merge request of the plan through the queue (or merged by a person on GitLab), and each
    repo the queue does not merge marked merged in the plan, is the deliver done; a revert is a failure; one blocked or
    paused waits for a person."""
    merges = view.merges.get(item.plan_id or "", ())
    reverted = [merge.id for merge in merges if merge.state == REVERTED]
    if reverted:
        return _failure(item, reverted=reverted)
    repos = [repo for repo in item.detail.get("repos") or [] if isinstance(repo, str)]
    by_hand = [repo for repo in repos if repo in view.manual]
    queued = [repo for repo in repos if repo not in view.manual]
    through = bool(merges) and all(_through(merge) for merge in merges)
    hand_done = all(repo in view.released.get(item.plan_id or "", frozenset()) for repo in by_hand)
    if hand_done and (through or (repos and not queued)):
        return _released(item, merges)
    held = sorted(merge.id for merge in merges if merge.state in MERGE_HELD)
    if held:
        reason, detail = "merge_blocked", {"merges": held}
    elif through or (repos and not queued):
        reason, detail = "merge_manual", {"by_hand": by_hand}
    else:
        reason, detail = "merge_queue", {}
    if item.reason == reason:
        return None
    state = WATCHING if reason == "merge_queue" else WAITING_HUMAN
    return Transition(item.id, state, WAITING, reason=reason, detail=detail)


def _deliver(item: ItemView, view: View) -> Transition | None:
    """A deliver's plan run, its decisions included, then the merge requests of its plan."""
    run = _run_of(item, view)
    if run is not None and (item.state == RUNNING or item.reason == "run_decision"):
        asking = _asks(item, run, "run_decision")
        if asking is not None or run.state != "done":
            return asking
        if run.steps_done is False:
            return _failure(item, run_id=run.id, why="the plan run ended with steps not done")
        return _merging(item, view, run)
    if item.reason in ("merge_queue", "merge_blocked", "merge_manual"):
        return _merged(item, view)
    return None


class Rules:
    """The engine of this release (``model.ENGINE``)."""

    name = ENGINE

    def rules(self, event: Event, view: View) -> list[Effect]:
        if event.kind == BASELINE:
            return _baseline(event, view)
        if event.kind == SRS_APPROVED:
            return _approved(event, view)
        return []

    def scheduled(self, view: View) -> list[Effect]:
        busy = {item.goal_id for item in view.items if item.kind == BRAINSTORM and not item.finished}
        effects: list[Effect] = []
        for goal in sorted(view.goals.values(), key=lambda goal: (goal.rank, goal.id)):
            if goal.status != ACTIVE or not goal.has_metric or goal.reached or goal.id in busy:
                continue
            spec = _brainstorm(view, goal)
            if spec is not None:
                effects.append(spec)
        return effects

    def reconcile(self, item: ItemView, view: View) -> Transition | None:
        if item.finished:
            return None
        stale = _stale(item, view) or _run_end(item, view)
        if stale is not None:
            return stale
        if item.state == WATCHING and item.reason == "triage":
            return _triaged(item, view)
        if item.state == WAITING_HUMAN and item.reason == "loop_decision":
            return None  # the person's answer moves it (``stall.answer``)
        return MOVES[item.kind](item, view)


MOVES = {BRAINSTORM: _brainstormed, USE_CASE: _drafted, BUILD: _build, DELIVER: _deliver}
RULES = Rules()

__all__ = ["RULES", "Rules"]
