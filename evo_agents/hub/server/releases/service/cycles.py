"""The moves of a release cycle that the check, the landing, the watch of its CI and its owner's answer share
(``evo_agents.hub.releases`` describes the cycle, ``evo_agents.hub.release_ci`` why it stops).

``queue_run`` queues the next run of an open cycle, a cut or the fix of a red tag, dispatched as the cycle's owner,
pinned to no worker, with no cost cap (``releases.run_budget``). Audited as release.dispatch, with the owner as actor
and no token. ``go_on`` is the next move of a cycle that goes on (the owner's continue, or ``releases.service.settle`` once the
branch a refused landing left is settled), and ``left_behind`` the run whose branch must be settled first.

``stop_cycle`` is the one way a cycle stops: open, it becomes stopped with the cause (``release_ci.STOP_CAUSES``) and
the reason, and the hub asks its owner what next (``ask_owner``): a decision of category release on the cycle's last
run, whose three options are to go on, to close the cycle, or to pause the schedule
(``releases.service.answer`` does the one chosen), notified to the owner on their channels; and a thread of the hub's in the
project's triage chat (``triage.project_thread``) that says why, the tags with their workflow runs, the cost and the
decision. The decision is the cycle's: the end of the run it hangs on leaves it open
(``runs.OUTLIVING_DECISION_CATEGORIES``). A stopped cycle moves no further until the owner answers, and no check opens
another cycle while it is there, unless a newer tag made outside it supersedes it (``releases.service.check.supersede``) or it
waits on no open decision any more (``releases.service.check.close_unanswerable``). Asking happens in a savepoint: a thread or a decision that cannot be made leaves the cycle stopped, saying so in
the log.

``notify_release`` sends the owner a notice of kind release: a version published, or a CI the hub cannot read.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, release_ci, releases, runs
from evo_agents.hub.server import audit
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.releases.models.cycles import (
    CLOSE_REASON,
    DISPATCH,
    OPTIONS,
    QUESTION_REASON_CHARS,
    releases_link,
)
from evo_agents.hub.server.releases.repository import cycles as cycles_repo
from evo_agents.hub.server.releases.repository import runs as runs_repo
from evo_agents.hub.server.releases.repository import tags as tags_repo
from evo_agents.hub.server.releases.repository.cycles import cycle_of
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.triage.service import threads as triage

log = logging.getLogger(__name__)


async def queue_run(conn: AsyncConnection, cycle_id: int, mode: str, *, red_tag: str | None = None) -> int | None:
    """Queue the next run of open cycle ``cycle_id`` in ``mode``, a fix of ``red_tag`` (see the module); its id, or
    None when another release run of the project is active."""
    found = await cycle_of(conn, cycle_id)
    budget = releases.run_budget()
    if mode == "fix":
        title = releases.fix_title(found.repo, red_tag or "")
    else:
        title = releases.cut_title(found.repo, found.base_tag, found.target_ref, found.target_commit or "")
    values = releases.run_values(
        project_id=found.project_id, owner_id=found.owner_id, repo=found.repo, title=title, budget=budget
    )
    try:
        async with conn.begin_nested():
            run_id = await insert_run(conn, values)
            await runs_repo.insert_release_run(conn, run_id, cycle_id, mode, tag=red_tag)
    except IntegrityError:  # runs_active_release_key: another release run of the project is active
        return None
    what = f"fixes {red_tag} forward with the next patch" if mode == "fix" else f"cuts {found.target_ref}"
    text = (
        f"Queued by cycle #{cycle_id} of the release of project {found.project}, as {found.owner}: it {what}, with "
        "no cost cap."
    )
    await write_event(conn, run_id, {"text": text, "cycle_id": cycle_id, "release_mode": mode, **budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=found.owner_id,
        token_id=None,
        action=DISPATCH,
        target=f"{found.project} cycle:{cycle_id} run:{run_id} mode={mode}",
        project_id=found.project_id,
    )
    return run_id


# Going on


async def newest_tag(conn: AsyncConnection, found):
    """The tag of highest version cycle ``found`` (``cycle_of``) follows, or None."""
    prefix = releases.tag_prefix(found.tag_pattern) + "*"
    rows = await tags_repo.tags_of(conn, found.id)
    versioned = [(releases.tag_version(row.tag, prefix) or (-1, -1, -1), row.id, row) for row in rows]
    return max(versioned, key=lambda item: item[:2])[2] if versioned else None


async def left_behind(conn: AsyncConnection, cycle_id: int):
    """The newest release run of cycle ``cycle_id`` when its landing left its branch release/<version> on origin:
    refused or failed, no tag made; None otherwise. The next run of the cycle would push that same branch (a fix takes
    the next patch of its red tag), so it waits for ``releases.service.settle``, which tags a merge of it a person made, or
    deletes the branch and then queues that run (2026-11-25, run #12580: a person merged release/0.73.4 as they answered
    continue, the run that answer queued pushed release/0.73.4 again and failed push_conflict)."""
    newest = await runs_repo.newest_run(conn, cycle_id)
    if newest is None or newest.branch is None or newest.tagged is not None:
        return None
    return newest if newest.land_state in ("refused", "failed") else None


async def held_by(conn: AsyncConnection, found):
    """The run (``left_behind``) whose branch holds the next move of cycle ``found``, when that move queues a run: its
    newest tag is red or it has none; None when nothing holds it."""
    tag = await newest_tag(conn, found)
    if tag is not None and tag.ci_state in (*release_ci.WATCHED, "timeout"):
        return None
    return await left_behind(conn, found.id)


async def go_on(conn: AsyncConnection, found) -> str:
    """The next move of open cycle ``found`` (``cycle_of``) that nothing holds, in words: the CI of its newest tag
    followed again from now when it was pending, running or overdue; else the fix of its red newest tag, or a new cut
    when it has no tag of its own yet, queued."""
    tag = await newest_tag(conn, found)
    if tag is not None and tag.ci_state in (*release_ci.WATCHED, "timeout"):
        await tags_repo.watch_again(conn, tag.id, "running" if tag.workflow_run_id else "pending")
        return f"the hub follows the CI of {tag.tag} again from now"
    mode, red = ("fix", tag.tag) if tag is not None and tag.ci_state == "failed" else ("cut", None)
    run_id = await queue_run(conn, found.id, mode, red_tag=red)
    if run_id is None:
        return f"no {mode} run could be queued: see the cycle"
    return f"run #{run_id} " + (f"fixes {red} forward" if red else f"cuts {found.target_ref} again")


# Stopping


async def stop_cycle(conn: AsyncConnection, cycle_id: int, reason: str, cause: str) -> bool:
    """Stop cycle ``cycle_id`` while it is open, keeping ``cause`` and ``reason``, and ask its owner what next;
    whether it stopped now."""
    stopped = await cycles_repo.stop(conn, cycle_id, reason, cause)
    if not stopped:
        return False
    log.warning("release cycle stopped", extra={"cycle_id": cycle_id, "cause": cause})
    try:
        async with conn.begin_nested():
            await ask_owner(conn, cycle_id)
    except Exception as exc:  # the stop holds even when the hub cannot ask
        log.warning("release stop not asked", extra={"cycle_id": cycle_id, "error": f"{type(exc).__name__}: {exc}"})
    return True


def _tag_line(row) -> str:
    where = f" ({row.workflow_url})" if row.workflow_url else ""
    tests = (row.failures or {}).get("ids") or []
    failed = f", failed: {', '.join(tests[:5])}" + (f" and {len(tests) - 5} more" if len(tests) > 5 else "")
    problem = f"; {row.problem}" if row.problem else ""
    return f"- {row.tag} at {releases.short(row.commit)}: CI {row.ci_state}{where}{failed if tests else ''}{problem}"


async def _facts(conn: AsyncConnection, found) -> tuple[list[str], int]:
    """The lines that say where stopped cycle ``found`` stands, and its last run."""
    tags = await tags_repo.tags_of(conn, found.id)
    items = await runs_repo.cycle_runs(conn, found.id)
    lines = [
        f"Release cycle #{found.id} of {found.project} stopped ({found.stop_cause}): {found.stop_reason}",
        f"Cost so far: {curator.money(float(found.cost))}.",
        "Tags it follows:" if tags else f"It made no tag since {found.base_tag or 'the first commit'}.",
        *(_tag_line(row) for row in tags),
        "Runs:",
        *(
            f"- #{item.run_id} {item.mode}{' of ' + item.tag if item.tag else ''}: {item.state}"
            + (f", tagged {item.tagged}" if item.tagged else f", {item.land_state}" if item.land_state else "")
            for item in items
        ),
    ]
    return lines, items[-1].run_id


async def ask_owner(conn: AsyncConnection, cycle_id: int) -> int:
    """Ask the owner of stopped cycle ``cycle_id`` what next (see the module); the decision's id."""
    found = await cycle_of(conn, cycle_id)
    lines, last_run = await _facts(conn, found)
    question = runs.clip(
        f"Release cycle #{cycle_id} of {found.project} stopped: "
        f"{NOTIFIER.one_line(found.stop_reason or '', QUESTION_REASON_CHARS)} Go on, close the cycle, or pause the release "
        "schedule?",
        runs.MAX_QUESTION_CHARS,
    )
    decision_id = await cycles_repo.insert_decision(
        conn,
        {
            "run_id": last_run,
            "project_id": found.project_id,
            "category": runs.RELEASE_DECISION_CATEGORY,
            "question": question,
            "context": runs.clip("\n".join(lines), runs.MAX_DECISION_CONTEXT_BYTES),
            "options": OPTIONS,
            "recommended_reason": CLOSE_REASON,
        },
    )
    await NOTIFIER.notify(
        conn,
        user_id=found.owner_id,
        kind="decision",
        project_id=found.project_id,
        run_id=last_run,
        decision_id=decision_id,
        title=f"Release cycle #{cycle_id} of {found.project} stopped: {NOTIFIER.one_line(found.stop_reason or '')}",
        body=f"{question}\n\n" + "\n".join(lines[1:]),
        link=NOTIFIER.decision_link(decision_id),
    )
    asked = (
        f"{found.owner} decides in decision #{decision_id} ({NOTIFIER.decision_link(decision_id)}): go on, close the cycle, or "
        "pause the release schedule. Nothing goes on until then."
    )
    thread = await triage.project_thread(
        conn, found.project_id, found.project, found.owner_id, "\n".join([*lines, asked])
    )
    await cycles_repo.set_asked(
        conn, cycle_id, decision_id, thread.chat_id if thread else None, thread.root_id if thread else None
    )
    log.info("release stop asked", extra={"cycle_id": cycle_id, "decision_id": decision_id})
    return decision_id


# Notices


async def notify_release(
    conn: AsyncConnection, found, *, event: str, title: str, body: str, run_id: int | None, details: dict
) -> int:
    """Send the owner of cycle ``found`` (``cycle_of``) a notice of kind release about ``event``."""
    return await NOTIFIER.notify(
        conn,
        user_id=found.owner_id,
        kind="notice",
        notice_kind="release",
        project_id=found.project_id,
        run_id=run_id,
        title=title,
        body=body,
        details={"event": event, "cycle_id": found.id, **details},
        link=releases_link(found.project),
    )
