"""What a decision carries besides its question and options, so the person asked can answer on a phone with one tap:
why the agent recommends the option it does (``recommended_reason``), whether the act it asks about can be undone and
how (``reversible``, ``undo``), and the refs of what the question rests on (``evidence``). All of it is optional: a
worker or a skill that predates it still asks.

The hub (``evo_agents.hub.server.decision_models``) and ``evo-agents worker ask`` both read evidence with
``evidence_ref``, which takes a ref in one of the forms of ``review.parse_evidence`` (``session:ID[:FIELD:INDEX]``,
``transcript:ID[:FILE]:LINE``, ``run:ID:SEQ``, ``code:REPO:PATH[:LINE]``, ``memory:ID:REV``) or ``run:ID``,
``output:ID``, ``P-<n>/E-<n>`` (a line of evidence of a problem) or an https URL, and gives it back as the command
line writes it. Only the form is checked: a ref names something the person may follow, not something the hub vouches
for.

ASK_RULE is the line of the prompts of plan runs and author runs that tells the agent to fill them. Standard library
only: the worker imports it without the hub-server extra.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_REASON_CHARS = 500  # why the agent recommends its option
MAX_UNDO_CHARS = 500  # how to undo the act a decision asks about
MAX_EVIDENCE = 20  # the refs of one decision
MAX_REF_CHARS = 2048  # one ref, an https URL the longest

RUN_REF = re.compile(r"^run:([1-9][0-9]{0,18})$")
OUTPUT_REF = re.compile(r"^output:([1-9][0-9]{0,18})$")
PROBLEM_REF = re.compile(r"^P-([1-9][0-9]{0,9})/E-([1-9][0-9]{0,9})$")
FORMS = (
    "run:ID, run:ID:SEQ, output:ID, P-<n>/E-<n>, an https URL, session:ID[:FIELD:INDEX], transcript:ID[:FILE]:LINE, "
    "code:REPO:PATH[:LINE] or memory:ID:REV"
)

ASK_RULE = (
    "- The person may answer on a phone, with one tap. Give every question that has a natural choice `--recommended "
    "KEY` and `--recommended-reason TEXT` (why that option, in a sentence or two); for an action, add `--reversible "
    "yes` with `--undo TEXT` (how to undo it) or `--reversible no`; and `--evidence REF` for each thing the question "
    "rests on, repeated: run:ID, output:ID, P-3/E-2, code:REPO:PATH or an https URL."
)


class RefProblem(ValueError):
    """A ref of a decision's evidence that is none of FORMS; ``str`` says why for the agent that wrote it."""


def _https(ref: str) -> bool:
    if any(ch.isspace() or not ch.isprintable() for ch in ref):
        return False
    try:
        parts = urlsplit(ref)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def evidence_ref(text: str) -> str:
    """``text`` as a ref of a decision's evidence, as the command line writes it (see the module); RefProblem when it
    is none of FORMS."""
    ref = (text or "").strip()
    if not ref or len(ref) > MAX_REF_CHARS:
        raise RefProblem(f"a ref of evidence has 1 to {MAX_REF_CHARS} characters: {FORMS}")
    if RUN_REF.match(ref) or OUTPUT_REF.match(ref) or PROBLEM_REF.match(ref):
        return ref
    if ref[:8].lower() == "https://":
        if not _https(ref):
            raise RefProblem(f"evidence {ref[:80]!r} is not an https URL with a host")
        return ref
    # here, not at the top: review imports runs, whose prompts import ASK_RULE from this module
    from evo_agents.hub.review import EVIDENCE_KINDS, EvidenceProblem, evidence_text, parse_evidence

    try:
        return evidence_text(parse_evidence(ref))
    except EvidenceProblem as exc:
        kind = ref.partition(":")[0]
        if kind == "run":
            raise RefProblem(f"run evidence is run:ID or run:ID:SEQ, whole numbers from 1, not {ref[:80]!r}") from None
        if kind in EVIDENCE_KINDS:  # its kind is right: what parse_evidence says of it helps more
            raise RefProblem(str(exc)) from None
        raise RefProblem(f"evidence is {FORMS}, not {ref[:80]!r}") from None
