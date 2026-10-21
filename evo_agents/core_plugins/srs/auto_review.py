"""@ba's own review of a version that enters review (criterion a7 of plan srs-tasks-plugins, criterion a6 of plan
srs-conflicts-and-tasks).

Every time a version of a document goes to review, in every round, from a member's submit or from @ba's proposal, and
the project enabled the agent type ``ba`` (which names the member its runs go under, ``run_as``), the plugin hands @ba
the review as a task run on a worker of that member (``task_dispatch.hand_own_task``). The task (``review_task``) and
the skill srs-review of @ba's pack tell it to:

- check the version against the template and write what is missing or wrong as comments with ``srs__comment``, which
  are advice: approving the version reads none of them;
- compare each use case the version changes with every use case of every document of the project, drafts included
  (``srs__search`` reads the latest version of each), and with the project's knowledge graph (``kg_search``,
  ``kg_context``, which the hub's /mcp offers the agent of a task run), going through actors, systems, repositories,
  seams, terms and business rules, each searched in Vietnamese and in English;
- record each conflict as a finding with the sources that show it (``srs__finding``, ``findings``); a conflict with
  no source is not recorded, and the hub refuses a source it cannot look up;
- settle a finding from the graph first when a Decision line of a plan, an approved SRS item or a newer document
  answers it: resolved with that source and no member, so no chat is needed for it;
- go over every open finding of the document (``srs__read`` lists them, and the task names them) and resolve those the
  version settles, citing the version as the source.

The run is kept as the review of the version's round (srs_versions.review_run_id, which a submit clears): approving
the version waits until it ends (``findings.approval_hold``). When the type is not enabled, is turned off, its run_as
no longer holds writer, or its runs spent the type's cap of the last 24 hours, the version goes to review all the
same, without @ba's review: the log says why, and the round has no review run for approval to wait on.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import BA, findings
from evo_agents.core_plugins.srs.store import PLUGIN, DocumentRow, VersionRow
from evo_agents.hub import tables
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.task_dispatch import hand_own_task

log = logging.getLogger(__name__)

MAX_COMMENTS = 20  # what one review writes at most
NAMED_FINDINGS = 50  # the open findings a task names by id; srs__read lists them all
CHECKLIST = (
    "the actors",
    "the systems",
    "the repositories",
    "the seams between them",
    "the terms of the glossary",
    "the business rules",
)


def _open_text(open_ids: Sequence[int]) -> str:
    if not open_ids:
        return "The document had no open finding when this review was handed."
    named = ", ".join(f"#{finding}" for finding in open_ids[:NAMED_FINDINGS])
    more = f" and {len(open_ids) - NAMED_FINDINGS} more" if len(open_ids) > NAMED_FINDINGS else ""
    return f"The document's open findings when this review was handed: {named}{more}."


def review_task(project: str, doc: DocumentRow, version: VersionRow, open_ids: Sequence[int] = ()) -> str:
    """The task of @ba's review of ``version`` of ``doc`` in ``project``, whose open findings are ``open_ids``."""
    number, slug = version.number, doc.slug
    again = (
        f", again (round {version.round}): it was sent back and submitted once more, and the other documents and "
        "the graph may have changed since"
        if version.round > 1
        else ""
    )
    if doc.approved_version:
        base = (
            f"The version approved last is {doc.approved_version}: see what changed with srs__diff (document {slug}, "
            f"from {doc.approved_version}, to {number}); the use cases it names, with the business rules under them, "
            "are the changed ones."
        )
    else:
        base = "No version of it was approved yet: every use case in it is a changed one."
    return "\n".join(
        [
            f'Review version {number} of the SRS document {slug} ("{doc.title}") of project {project}, which '
            f"{version.author} sent to review{again}. {base}",
            "",
            "Follow the skill srs-review of your pack:",
            f"1. Read the version with srs__read (document {slug}, version {number}); it lists the document's open "
            "findings too.",
            "2. Check it against the template: the headings and codes it asks for, each use case's actor, "
            "preconditions, steps, expectation or post-conditions and business rules, the screens a use case names. "
            "Write what is missing or wrong "
            f"as comments with srs__comment (document {slug}, version {number}, item: the code or key of the item it "
            f'is about), each starting with "Blocking:" or "Suggestion:", at most {MAX_COMMENTS}; one about the whole '
            f"document goes on version {number} without an item. Comments are advice: they block no approval.",
            "3. Compare each changed use case with every use case of every document of the project, drafts and "
            "versions in review included (srs__search reads the latest version of each document), with the glossary "
            "and with the non-functional requirements; then with the project's knowledge graph: kg_search finds "
            "nodes by words, kg_context shows a node with what it links to. Go through this checklist, and search each "
            f"entry in Vietnamese and in English (khách hàng and customer, đơn hàng and order): {', '.join(CHECKLIST)}.",
            f"4. Record each conflict as one finding with srs__finding (action record, document {slug}, version "
            f"{number}, item when it is about one): its kind (duplicate, contradiction, kg_conflict, overlap), its "
            "level (decide when members must choose, advisory when they should look, info otherwise), a summary in "
            "one sentence, and the sources that show it: the code of another item (code), the id of a node kg_search "
            "gave (node), or repo:path of a file or document (path). The hub looks each source up and refuses a "
            "finding with one it cannot find. A conflict with no source is no finding: do not record it. Do not "
            "record again a conflict an open finding already holds.",
            "5. Try to settle each finding from the graph before you leave it open: a Decision line of a plan, an "
            "approved item of an SRS document, or a newer document that answers it. When one does, resolve the "
            "finding at once (action resolve, the finding's id, a resolution that says how, the sources that settle "
            "it, and no settled_by). A finding settled from the graph needs no chat: members are asked only about "
            "findings of level decide still open when your run ends.",
            f"6. Go over every open finding of the document. {_open_text(open_ids)} When version {number} "
            f'settles one found on an earlier version, resolve it with the source {{"kind": "version", "ref": '
            f'"{number}"}} and a resolution that says how; settle any other from the graph as in step 5, or leave it '
            "open.",
            "",
            "Your findings and comments are all you write. An open finding of level decide holds the version's "
            "approval until members settle it; your comments hold nothing. You approve, return and propose nothing, "
            "you create no task, you open no chat, and you change no file of the repositories of this run.",
        ]
    )


async def review_entered(conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, version: VersionRow):
    """Hand @ba the review of ``version`` of ``doc``, which entered review now (in any round), in the caller's
    transaction; the task run, or None when there is none to start (see the module)."""
    if version.state != "in_review":
        return None
    again = f", round {version.round}" if version.round > 1 else ""
    title = f"@{BA} reviews {doc.slug} v{version.number}{again}"
    open_ids = [row.id for row in await findings.findings(conn, doc, findings.OPEN)]
    task = review_task(access.name, doc, version, open_ids)
    run_id, why = await hand_own_task(conn, PLUGIN, access.project_id, BA, task, title)
    if run_id is None:
        extra = {"project": access.name, "document": doc.slug, "version": version.number, "why": why}
        log.info("no review by @ba", extra=extra)
        return None
    v = tables.srs.versions
    await conn.execute(update(v).where(v.c.id == version.id).values(review_run_id=run_id))
    log.info("review by @ba queued", extra={"project": access.name, "document": doc.slug, "run_id": run_id})
    return run_id


__all__ = ["CHECKLIST", "MAX_COMMENTS", "NAMED_FINDINGS", "review_entered", "review_task"]
