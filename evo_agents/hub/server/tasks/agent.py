"""The tool of the plugin tasks on the hub's /mcp: ``tasks__create``, a task an agent makes for the member who asked
it, such as @ba making a task of a use case (criterion a7 of plan srs-tasks-plugins), or a child task of a frame task an
approval made (criterion a8 of plan srs-conflicts-and-tasks).

The hub lists it in a session whose project enabled the plugin, and checks its arguments against the manifest's
schema first. It acts for a person: the agent of a chat run for the person who mentioned the agent, the origin of the
chain (``chat_runs.ChatRun.dispatcher_id``), also when the run went to the worker of its agent type's run_as; the agent
of the task run of @ba that splits the frame tasks of an approved SRS version or use case
(``hub.server.srs.service.auto_tasks``) for the member whose approval made it approved, whom the hub keeps for that run
(srs_approval_tasks.approved_by by split_run_id, or srs_use_case_releases.released_by by the split run its ``tasks``
keeps; the task's first run, which a lost attempt's next goes on from); a member's own session (no run) for
that member. The agent of any other run gets a refusal: nobody asked it for a task. The person must hold writer on
the project when the tool runs. The task is theirs, carries the project's label, takes the config's first type and
status unless named, and links the SRS items it names by code while the project enables srs, through the session's
sink. A build of the project's knowledge graph is queued, as after a task made with the API.

``parent`` puts the task under another task of the project, as T-12. A child whose title another child of the same
parent has, compared with case and runs of whitespace folded, is refused, so a split run tried again makes no twin; the
parent's row is locked first, so two calls cannot both pass. The split run always names a parent, and only a frame
task (an external_key that starts with ``tables_srs.FRAME_TASK_PREFIX``); its child tasks link the use cases the frame
task links too, besides those the call names.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.tasks.models.values import TasksError
from evo_agents.hub.server.tasks.service import agents

log = logging.getLogger(__name__)

RACE = "another task of the project took that number at the same moment: nothing was written; try again"


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


async def create(params: dict, context: CoreContext) -> dict:
    """The tool ``tasks__create``: a new task for the member the agent works for, under ``parent`` when named."""
    assert context.engine is not None  # the hub's /mcp always has its engine
    try:
        async with UnitOfWork(context.engine) as conn:
            made = await agents.create(conn, params, context)
            log.info(
                "task created by an agent", extra={"project": made.project, "task": made.ref, "run_id": context.run_id}
            )
            return _text(made.text)
    except TasksError as exc:
        return _text(f"error: {exc.message}", error=True)
    except HTTPException as exc:
        return _text(f"error: {exc.detail}", error=True)
    except IntegrityError:
        return _text(f"error: {RACE}", error=True)


__all__ = ["create"]
