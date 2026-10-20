"""@ba's own review of a version that enters review (criterion a7 of plan srs-tasks-plugins).

When a version of a document goes to review for the first time (its first round, from a member's submit or from
@ba's proposal), and the project enabled the agent type ``ba`` (which names the member its runs go under,
``run_as``), the plugin hands @ba the review as a task run on a worker of that member
(``task_dispatch.hand_own_task``): read the version, check it against the template, find where its use cases
contradict each other, the glossary or the non-functional requirements, and write each finding as a comment on its
item with ``srs__comment``. The comments are advice: approving the version waits for nobody's comment and reads none.
The run is kept as the review of the version's round (srs_versions.review_run_id): approving the version waits until
it ends (``findings.approval_hold``).

A version sent back and submitted again is the same content, so it is not reviewed again. When the type is not
enabled, is turned off, its run_as no longer holds writer, or its runs spent the type's cap of the last 24 hours, the
version goes to review all the same, without @ba's review; the log says why.
"""

from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import BA
from evo_agents.core_plugins.srs.store import PLUGIN, DocumentRow, VersionRow
from evo_agents.hub import tables
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.task_dispatch import hand_own_task

log = logging.getLogger(__name__)

MAX_COMMENTS = 20  # what one review writes at most


def review_task(project: str, doc: DocumentRow, version: VersionRow) -> str:
    """The task of @ba's review of ``version`` of ``doc`` in ``project``."""
    number, slug = version.number, doc.slug
    if doc.approved_version:
        base = (
            f"The version approved last is {doc.approved_version}: see what changed with srs__diff (document {slug}, "
            f"from {doc.approved_version}, to {number}), and review the changed items first."
        )
    else:
        base = "No version of it was approved yet: review it whole."
    return "\n".join(
        [
            f'Review version {number} of the SRS document {slug} ("{doc.title}") of project {project}, which '
            f"{version.author} sent to review. {base}",
            "",
            "Follow the skill srs-review of your pack:",
            f"1. Read the version with srs__read (document {slug}, version {number}).",
            "2. Check it against the template: the headings and codes it asks for, each use case's actor, "
            "preconditions, steps and business rules, the screens a use case names.",
            "3. Find contradictions: between use cases, between a use case and the glossary, and between a use case "
            "and the non-functional requirements.",
            f"4. Write each finding as one comment with srs__comment (document {slug}, version {number}, item: the "
            'code or key of the item it is about), starting with "Blocking:" or "Suggestion:". A finding about the '
            f"whole document goes on version {number} without an item. Write at most {MAX_COMMENTS} comments; when "
            f"you find nothing, write one comment on version {number} that says so.",
            "",
            "Your comments are advice: they block no approval, and the members of the project approve or return the "
            "version. You approve, return and propose nothing, you create no task, and you change no file of the "
            "repositories of this run.",
        ]
    )


async def review_entered(conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, version: VersionRow):
    """Hand @ba the review of ``version`` of ``doc``, which entered review now, in the caller's transaction; the task
    run, or None when there is none to start (see the module)."""
    if version.state != "in_review" or version.round != 1:
        return None
    title = f"@{BA} reviews {doc.slug} v{version.number}"
    task = review_task(access.name, doc, version)
    run_id, why = await hand_own_task(conn, PLUGIN, access.project_id, BA, task, title)
    if run_id is None:
        log.info("no review by @ba", extra={"project": access.name, "document": doc.slug, "why": why})
        return None
    v = tables.srs.versions
    await conn.execute(update(v).where(v.c.id == version.id).values(review_run_id=run_id))
    log.info("review by @ba queued", extra={"project": access.name, "document": doc.slug, "run_id": run_id})
    return run_id


__all__ = ["MAX_COMMENTS", "review_entered", "review_task"]
