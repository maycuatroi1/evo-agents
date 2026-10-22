"""What the commands of a run's agent read (``evo_agents.worker.cli``): a text from a file or stdin, within a bound,
an option of a decision, and the evidence specs of a finding or a proposal (``evo_agents.hub.review.parse_evidence``).
A problem is a WorkerStateError, which the command prints as one ``error:`` line."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from evo_agents.hub import runs
from evo_agents.worker.home import WorkerStateError


def read_text(source: str, what: str, limit: int) -> str | None:
    """The text of file ``source`` (``-``: stdin), at most ``limit`` bytes of UTF-8; None when it holds only blanks."""
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkerStateError(f"cannot read {source}: {exc}") from None
    if len(text.encode()) > limit:
        raise WorkerStateError(f"the {what} is at most {limit} bytes of UTF-8")
    return text if text.strip() else None


def decision_option(text: str) -> dict:
    """``KEY=LABEL[:DESCRIPTION]`` as an option of a decision (``worker ask --option``)."""
    key, sep, rest = text.partition("=")
    label, _, description = rest.partition(":")
    key, label, description = key.strip(), label.strip(), description.strip()
    if not sep or not label or not re.fullmatch(runs.OPTION_KEY, key):
        raise WorkerStateError(
            f"--option takes KEY=LABEL[:DESCRIPTION], KEY being letters, digits, _ and - (at most 32), not {text!r}"
        )
    return {"key": key, "label": label, **({"description": description} if description else {})}


def evidence_items(texts: list[str]) -> list[dict]:
    """Each evidence spec of ``texts`` parsed, in order; the first that does not parse is the error."""
    from evo_agents.hub.review import EvidenceProblem, parse_evidence

    found = []
    for text in texts:
        try:
            found.append(parse_evidence(text))
        except EvidenceProblem as exc:
            raise WorkerStateError(str(exc)) from None
    return found
