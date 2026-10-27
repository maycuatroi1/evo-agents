"""What the Loop makes of the backlog of a goal that aims at an SRS document (plan srs-backlog-loop, criterion a3): a
pure function over a ``View``, read by ``rules2`` on each tick; nothing here reads a table or writes.

``View.backlogs`` holds the groups of the backlog of each active goal whose baseline an admin confirmed (a goal that is
not confirmed has none, so no build is made for it). Each group gets one ``build`` of cycle development, key
``build:srs:<document id>:<group key>:<round>``, whose detail names the group and its use cases (code, title, version)
under ``backlog``; ``srs_document_id`` of the work item is the document. The author run it gets writes one plan for all of
them (``texts.srs_request``) and a ``deliver`` of that plan goes on as rows 9 to 11 of rules/1 do. While a build or its
deliver is not over, its use cases are ``delivering`` in the ledger and so out of the backlog (``srs_ledger``), so a group
has at most one build under way; a use case approved into the group meanwhile is in the next round, which is the number
of builds of that group made before, plus one, finished and cancelled ones included. A tick of a Loop enabled late reads
the backlog as it is: a use case approved before the Loop came on is in a build as any other.
"""

from __future__ import annotations

from evo_agents.hub.server.loop.model import ACTIVE, BUILD, QUEUED, SRS_BUILD, View, WorkSpec
from evo_agents.hub.srs_backlog import Group


def build_key(document_id: int, group: Group, round_: int) -> str:
    return f"{SRS_BUILD}{document_id}:{group.key}:{round_}"


def next_round(keys: frozenset[str], document_id: int, group: Group) -> int:
    """The round of the next build of ``group``: one more than the builds of it already made."""
    prefix = f"{SRS_BUILD}{document_id}:{group.key}:"
    made = [key[len(prefix) :] for key in keys if key.startswith(prefix)]
    return 1 + sum(1 for rest in made if rest.isdigit())


def srs_builds(view: View) -> list[WorkSpec]:
    """A build for each group of the backlog of each active goal, in the order of the goals' rank."""
    found: list[WorkSpec] = []
    for goal in sorted(view.goals.values(), key=lambda goal: (goal.rank, goal.id)):
        if goal.status != ACTIVE or goal.srs_document_id is None:
            continue
        for group in view.backlogs.get(goal.id, ()):
            round_ = next_round(view.srs_keys, goal.srs_document_id, group)
            detail = {
                "backlog": {
                    "document_id": goal.srs_document_id,
                    "group": group.key,
                    "title": group.title,
                    "round": round_,
                    "use_cases": [
                        {"code": case.ref, "title": case.title, "version": case.version} for case in group.use_cases
                    ],
                }
            }
            key = build_key(goal.srs_document_id, group, round_)
            found.append(WorkSpec(BUILD, key, goal.id, QUEUED, srs_document_id=goal.srs_document_id, detail=detail))
    return found


__all__ = ["build_key", "next_round", "srs_builds"]
