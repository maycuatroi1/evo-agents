"""The plugin's own handlers of the events the hub delivers to it, each in a transaction of its own:

- ``review_ended``, the contribution ``reviews``: when @ba's review of a version ends, its settle chat
  (``service.settle``) and the reviewers that wait for it;
- ``on_version_approved``, the contribution ``approvals``: the tasks of an approved version (``service.auto_tasks``);
- ``on_use_case_approved``, the contribution ``releases``: the tasks of a released use case.

``service.events`` says what each event carries and when the hub delivers it.
"""

from __future__ import annotations

from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.srs import BA
from evo_agents.hub.server.srs.repository import store
from evo_agents.hub.server.srs.service import auto_tasks, events, settle


async def review_ended(params: dict, context: CoreContext) -> dict:
    """The contribution ``reviews``: ``params`` is plugin/1's ``event`` of a task run of the plugin's agent types."""
    payload = params.get("payload") or {}
    task_id = payload.get("task_id")
    if params.get("kind") != "task.result" or payload.get("agent_type") != BA or not isinstance(task_id, int):
        return {}
    if context.engine is None or context.project_id is None:
        return {}
    async with UnitOfWork(context.engine) as conn:
        await settle.open_chat(conn, context.project_id, task_id)
        await settle.tell_reviewers(conn, context.project_id, task_id)
    return {}


def _ids(payload: dict) -> tuple[int, int, int | None, int] | None:
    """(document, version, version approved before, approver) of a payload, or None when one is not an id."""
    document_id, version = payload.get("document_id"), payload.get("version")
    previous, approver_id = payload.get("previous"), payload.get("approver_id")
    if not (isinstance(document_id, int) and isinstance(version, int) and isinstance(approver_id, int)):
        return None
    if previous is not None and not isinstance(previous, int):
        return None
    return document_id, version, previous, approver_id


async def on_use_case_approved(params: dict, context: CoreContext) -> dict:
    """The contribution ``releases``: ``params`` is plugin/1's ``event`` (the payload of
    ``events.use_cases_released``). A document of another project than the delivery's is left alone, and an event of
    an older shape (no release) too."""
    payload = params.get("payload") or {}
    release_id, document_id = payload.get("release_id"), payload.get("document_id")
    if params.get("kind") != events.USE_CASE_APPROVED or context.engine is None or context.project_id is None:
        return {}
    if not isinstance(release_id, int) or not isinstance(document_id, int):
        return {}
    async with UnitOfWork(context.engine) as conn:
        if await store.project_of_document(conn, document_id) != context.project_id:
            return {}
        batch = [one for one in payload.get("batch") or [] if isinstance(one, int)]
        await auto_tasks.released(conn, release_id, batch or [release_id])
    return {}


async def on_version_approved(params: dict, context: CoreContext) -> dict:
    """The contribution ``approvals``: ``params`` is plugin/1's ``event`` (the payload of ``events.version_approved``
    and its delivery). A document of another project than the delivery's is left alone."""
    ids = _ids(params.get("payload") or {})
    if params.get("kind") != events.APPROVED or ids is None or context.engine is None or context.project_id is None:
        return {}
    document_id, version, previous, approver_id = ids
    async with UnitOfWork(context.engine) as conn:
        if await store.project_of_document(conn, document_id) != context.project_id:
            return {}
        if not await events.tasks_since(conn, context.project_id, document_id, version):
            await auto_tasks.approved(conn, document_id, version, previous, approver_id)
    return {}


__all__ = ["on_use_case_approved", "on_version_approved", "review_ended"]
