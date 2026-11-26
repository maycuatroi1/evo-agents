"""How the outputs of runs read (``evo_agents.hub.outputs``): the outputs of a run, of questions and of chat messages,
each as a ``models.RunOutput`` with the URL its reader fetches the bytes from.

An output's ``url`` is a path of this hub that answers its bytes to whoever may read what listed it: the run's
readers for the outputs of a run (GET /v1/projects/{p}/runs/{id}/outputs/{oid}/content), the decision's readers for
the outputs a question carries (GET /v1/projects/{p}/decisions/{id}/outputs/{oid}/content), and the chat's members
who read the message for those a chat message carries (GET /v1/chats/{id}/outputs/{oid}/content). ``?download=true``
asks for it as an attachment (``content``). A notification lists the outputs of its decision with the decision's URLs,
and a notice of a run's files (``runs.OUTPUT_NOTICE``) those its details name with the run's (``by_notification``).
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs
from evo_agents.hub.server.outputs.models import RunOutput, chat_url, decision_url, run_url, view
from evo_agents.hub.server.outputs.repository import views as repository


async def of_run(
    conn: AsyncConnection, project: str, run_id: int, sources: list[str] | None = None, after: int = 0
) -> list[RunOutput]:
    """The outputs of run ``run_id`` of ``project`` after id ``after``, of ``sources`` (every source when empty or
    None), oldest first, each with the run's URL of its bytes."""
    rows = await repository.of_run(conn, run_id, sources, after)
    return [view(row, run_url(project, run_id, row.id)) for row in rows]


async def by_decision(conn: AsyncConnection, project: str, decision_ids: list[int]) -> dict[int, list[RunOutput]]:
    """The outputs each of ``decision_ids`` carries, oldest first, each with that decision's URL of its bytes."""
    if not decision_ids:
        return {}
    found: dict[int, list[RunOutput]] = defaultdict(list)
    for row in await repository.by_decision(conn, decision_ids):
        found[row.decision_id].append(view(row, decision_url(project, row.decision_id, row.id)))
    return found


async def by_message(conn: AsyncConnection, chat_id: int, message_ids: list[int]) -> dict[int, list[RunOutput]]:
    """The outputs each of ``message_ids`` of chat ``chat_id`` carries, oldest first, each with the chat's URL of its
    bytes."""
    if not message_ids:
        return {}
    found: dict[int, list[RunOutput]] = defaultdict(list)
    for row in await repository.by_message(conn, message_ids):
        found[row.chat_message_id].append(view(row, chat_url(chat_id, row.id)))
    return found


def _named(details) -> list[int]:
    named = details.get("outputs") if isinstance(details, dict) else None
    return [value for value in named or [] if isinstance(value, int)]


async def by_notification(conn: AsyncConnection, notifications: list) -> dict[int, list[RunOutput]]:
    """The outputs each of ``notifications`` (with ``id``, ``kind``, ``notice_kind``, ``project``, ``run_id``,
    ``decision_id`` and ``details``, as ``notifications.Notification`` has them) carries, by its id: a decision's,
    with the decision's URLs, and those a notice of a run's files names that are still there, with the run's."""
    asked: dict[str, list] = defaultdict(list)  # the notifications of decisions, by project
    noticed = []
    for item in notifications:
        if item.project is None:
            continue
        if item.kind == "decision" and item.decision_id is not None:
            asked[item.project].append(item)
        elif item.notice_kind == runs.OUTPUT_NOTICE and item.run_id is not None and _named(item.details):
            noticed.append(item)
    found: dict[int, list[RunOutput]] = {}
    for project, items in asked.items():
        carried = await by_decision(conn, project, [item.decision_id for item in items])
        found |= {item.id: carried[item.decision_id] for item in items if item.decision_id in carried}
    ids = sorted({output_id for item in noticed for output_id in _named(item.details)})
    if ids:
        rows = await repository.by_ids(conn, ids)
        for item in noticed:
            named = sorted(i for i in set(_named(item.details)) if i in rows and rows[i].run_id == item.run_id)
            if named:
                found[item.id] = [view(rows[i], run_url(item.project, item.run_id, i)) for i in named]
    return found
