"""The notices of the worker: POST /v1/worker/runs/{id}/notices takes a notice (``runs.NOTICE_KINDS``) from the
worker holding a plan run: a title, a body, and the repo, branch and commits of a push or merge. It notifies the run's
owner, links to the run's page and leaves a ``system`` event in the run's log. A run of one step sends no notice (404,
as any run the worker does not hold). The hub sends ``plan_finished`` and ``run_failed`` itself when a plan run ends
(``run_notices``)."""

from __future__ import annotations

import logging

from fastapi import HTTPException

from evo_agents.hub.server.notifications.models import NoticeIn, Notification
from evo_agents.hub.server.notifications.repository import listing
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


async def send_notice(engine, user: Principal, run_id: int, body: NoticeIn) -> Notification:
    """Notify the owner of plan run ``run_id``, which the worker of ``user`` holds, of ``body``; the notification as
    its owner reads it. 422 for a repo the run does not work in."""
    async with engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True)
        _, _, _, project_id, project, plan_id, dispatcher_id, _, repos = row
        names = [
            entry["repo"] for entry in repos or [] if isinstance(entry, dict) and isinstance(entry.get("repo"), str)
        ]
        if body.repo is not None and body.repo not in names:
            raise HTTPException(422, f"run {run_id} works in {', '.join(names) or 'no repo'}, not in {body.repo}")
        notification_id = await NOTIFIER.notify(
            conn,
            user_id=dispatcher_id,
            kind="notice",
            notice_kind=body.kind,
            project_id=project_id,
            run_id=run_id,
            title=body.title,
            body=body.body,
            details=body.details(),
            link=NOTIFIER.run_link(project, run_id),
        )
        shown = f"notice {body.kind}: {body.title}"
        facts = {"kind": body.kind, "notification_id": notification_id, **(body.details() or {})}
        await write_event(conn, run_id, {"text": shown, "notice": facts})
        view = await listing.notification(conn, notification_id)
    log.info("notice sent", extra={"run_id": run_id, "kind": body.kind, "plan_id": plan_id})
    return view
