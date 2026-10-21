"""The rules a claim's status follows, checked in code on every path that changes it (an edit on the web, the command
line or the API, an import, an undo, a proposal and its acceptance, the tools of @strategist; later the decisions on a
baseline). An agent never moves a claim to validated, whoever asked it to.

A claim goes to validated only with at least one piece of real evidence that supports it. Real evidence is not
synthetic, of a kind other than doc and simulation, not withdrawn, and added by a person; or added by an agent and
confirmed by a writer; or written by the hub (the value check of a signal or the judging of feedback). A claim goes to
testing only with a test card that holds a hypothesis, a test and a deadline. Going to invalidated or retired takes
nothing: dropping a hypothesis is cheap and can be undone. A refusal is a 422 that says what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.core_plugins.canvas.content import CanvasError, Content, code

NOT_EVIDENCE_KINDS = ("doc", "simulation")  # kinds that never validate a claim
HUB_KINDS = ("signal", "feedback")  # the kinds only the hub writes: value check and the judging of feedback
TESTING_NEEDS = ("hypothesis", "test", "deadline")  # of the test card of a claim in testing


@dataclass(frozen=True)
class Evidence:
    """What the rule reads of one piece of evidence."""

    id: int
    kind: str
    direction: str
    synthetic: bool
    actor: str
    confirmed: bool
    withdrawn: bool


def not_real(evidence: Evidence) -> str | None:
    """Why ``evidence`` is no real evidence, in words; None when it is real."""
    if evidence.withdrawn:
        return "it was withdrawn"
    if evidence.synthetic:
        return "it is synthetic"
    if evidence.kind in NOT_EVIDENCE_KINDS:
        return f"it is of kind {evidence.kind}"
    if evidence.actor == "agent" and not evidence.confirmed:
        return "an agent added it and no writer confirmed it"
    if evidence.actor == "hub" and evidence.kind not in HUB_KINDS:
        return f"the hub writes evidence of kind {', '.join(HUB_KINDS)} only"
    return None


def real_support(evidence: list[Evidence]) -> list[Evidence]:
    """The real evidence among ``evidence`` that supports its claim."""
    return [item for item in evidence if item.direction == "supports" and not_real(item) is None]


def _validated(number: int, evidence: list[Evidence]) -> None:
    if real_support(evidence):
        return
    supporting = [item for item in evidence if item.direction == "supports"]
    if not supporting:
        why = "no evidence supports it"
    else:
        why = "; ".join(f"evidence {item.id}: {not_real(item)}" for item in supporting)
    raise CanvasError(
        422,
        f"{code(number)} goes to validated only with real evidence that supports it (not synthetic, of a kind other "
        f"than {' or '.join(NOT_EVIDENCE_KINDS)}, not withdrawn, added by a person or confirmed by a writer): {why}",
    )


def _testing(number: int, content: Content) -> None:
    card = content.test_card or {}
    lacking = [name for name in TESTING_NEEDS if not card.get(name)]
    if lacking:
        raise CanvasError(
            422,
            f"{code(number)} goes to testing only with a test card that holds a hypothesis, a test and a deadline; "
            f"it lacks {', '.join(lacking)}",
        )


def check(number: int, before: Content | None, after: Content, evidence: list[Evidence], actor: str = "person") -> None:
    """Refuse moving claim ``number`` from ``before`` (None for a new claim) to ``after`` when its status breaks a
    rule: validated without real supporting ``evidence``, or by an agent (``actor``) at all, even when a member asked
    for it; testing without the test card it needs."""
    moved = before is None or before.status != after.status
    if after.status == "validated" and moved and actor == "agent":
        raise CanvasError(
            403,
            f"an agent never moves {code(number)} to validated, even when a member asks: a writer does, on the web or "
            "the command line, with real evidence that supports it",
        )
    if after.status == "validated" and moved:
        _validated(number, evidence)
    card_changed = before is None or before.test_card != after.test_card
    if after.status == "testing" and (moved or card_changed):
        _testing(number, after)
