"""The baseline of a goal that aims at an SRS document (plan srs-backlog-loop, criterion a5): what the hub proposes of each
approved use case, from what the knowledge graph shows, and how an admin's fixes apply. Pure functions and constants,
standard library only, so the Loop, the api and the command line share them.
``evo_agents.hub.server.loop.baseline`` reads the use cases, the graph and the ledger and writes the confirmation.

Each approved use case is proposed as one of three states (``evo_agents.hub.srs_backlog``):

- ``delivered`` when the graph holds evidence of it: a plan whose steps are all done, or a step done, that mentions its
  code; a commit or a merged pull request that mentions it; a comment in the code that mentions it;
- ``undelivered`` when the graph holds none and the use case is newer than the baseline (a review approved it after the
  goal began to aim at the document), so nothing could have been built for it yet;
- ``unknown`` in every other case: it may be delivered without a trace, and a person says. An unknown use case never
  enters the backlog by itself.

Evidence is what the graph says (``Evidence``): a kind of ``EVIDENCE_KINDS``, the node it is read from and its title.
The admin confirms the baseline once; before that the Loop makes no build for the goal (``confirmed`` of
``evo_agents.hub.server.loop.baseline``). Fixes are by code, by heading (every use case under it) and for the whole of
the unknown ones, to ``delivered`` or ``undelivered`` (``FIX_STATES``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from evo_agents.hub.srs_backlog import DELIVERED, UNDELIVERED, UNKNOWN, UseCase, ref_key, under

PLAN_COMPLETED, STEP_DONE, COMMIT, PULL_REQUEST, CODE_COMMENT = EVIDENCE_KINDS = (
    "plan_completed",
    "step_done",
    "commit_merged",
    "pr_merged",
    "code_comment",
)
FIX_STATES = (DELIVERED, UNDELIVERED)  # what an admin sets a use case to
MAX_EVIDENCE = 5  # lines of evidence one use case shows


@dataclass(frozen=True)
class Evidence:
    """One thing the graph shows of a use case: ``ref`` is the id of the node it is read from."""

    kind: str
    ref: str
    title: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "title": self.title}


@dataclass(frozen=True)
class Proposal:
    """The state the hub proposes for a use case, with the evidence and the reason in a line."""

    state: str
    evidence: tuple[Evidence, ...] = ()
    reason: str = ""


def propose(evidence: Iterable[Evidence], newer: bool) -> Proposal:
    """The proposal for a use case that has ``evidence`` in the graph and is ``newer`` than the baseline or not."""
    found = tuple(evidence)
    if found:
        kinds = sorted({item.kind for item in found})
        return Proposal(DELIVERED, found[:MAX_EVIDENCE], f"the graph holds {len(found)}: {', '.join(kinds)}")
    if newer:
        return Proposal(UNDELIVERED, (), "approved after the baseline, and nothing in the graph mentions it")
    return Proposal(UNKNOWN, (), "approved before the baseline, and nothing in the graph mentions it")


def fixes(
    states: Mapping[str, str],
    cases: Iterable[UseCase],
    *,
    codes: Mapping[str, str] | None = None,
    headings: Mapping[str, str] | None = None,
    unknown: str | None = None,
) -> dict[str, str]:
    """The states an admin's fix sets, by ``ref_key``, given the current ``states`` of the use cases ``cases``: first
    the whole of the unknown ones, then each heading, then each code, so the narrower one wins. A code no use case of
    ``cases`` has raises KeyError naming it."""
    held = list(cases)
    wanted = {ref_key(case.ref): case for case in held}
    out: dict[str, str] = {}
    if unknown:
        out.update({key: unknown for key, state in states.items() if state == UNKNOWN and key in wanted})
    for heading, state in (headings or {}).items():
        out.update({ref_key(case.ref): state for case in under(held, heading)})
    for code, state in (codes or {}).items():
        key = ref_key(code)
        if key not in wanted:
            raise KeyError(code)
        out[key] = state
    return out


def counts(states: Iterable[str]) -> dict[str, int]:
    """How many use cases are in each state of ``evo_agents.hub.srs_backlog.STATES`` that has one."""
    found: dict[str, int] = {}
    for state in states:
        found[state] = found.get(state, 0) + 1
    return found


__all__ = [
    "CODE_COMMENT",
    "COMMIT",
    "EVIDENCE_KINDS",
    "FIX_STATES",
    "MAX_EVIDENCE",
    "PLAN_COMPLETED",
    "PULL_REQUEST",
    "STEP_DONE",
    "Evidence",
    "Proposal",
    "counts",
    "fixes",
    "propose",
]
