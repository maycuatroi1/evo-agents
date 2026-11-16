"""The playbook of the Curator: what the review runs of a project learned of it, kept as entries that change by deltas
alone, never written anew, as Agentic Context Engineering keeps its playbook (Zhang et al. 2025): a context written
again whole loses its details a little more each time ("context collapse"), and a list of entries, each with its id, its
evidence and its counters of helpful and harmful, keeps them.

An entry has an id (``pb-<n>`` as the prompt and the command line write it), a lens of ``review.LENSES`` or none (an
entry for every lens), its text (one lesson, at most MAX_TEXT_CHARS), the evidence it was added with, and two counters:
how often a review run found it helpful, and harmful. A delta is one of OPS:

- ``add``: a new entry, with its text and at least one piece of evidence;
- ``helpful`` and ``harmful``: one more on its counter, with the evidence of why when there is some; a review run counts
  each entry once each way;
- ``retire``: it no longer holds, with a note why; it stays, out of the prompt.

The hub keeps every delta (``evo_agents.hub.server.playbook``), and the prompt of the next review run shows the live
entries of its lenses and those of no lens, the most helpful first (``prompt_lines``). Pure functions, standard library
only, shared by the hub and the agent's command ``evo-agents worker playbook``.

An entry may hold only until a proposal lands: ``until_proposal_id`` names it, given with ``--until-proposal`` or read
from the text of a live entry that says so (``until_proposal``: ``until proposal #12 lands``, ``until #12 lands``, ``#12
lands``). Each review run that ends counts once on ``prompt_exposures`` of every entry its prompt showed. The hub's
maintenance (``due_retires``) retires an entry, as actor CURATOR_ACTOR, once its proposal is merged, or once it was
shown ``exposure_limit`` times (EXPOSURE_LIMIT unless the charter says) without ever being found helpful.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

OPS = ("add", "helpful", "harmful", "retire")
TAGS = ("helpful", "harmful")
PREFIX = "pb-"
MAX_TEXT_CHARS = 500
MAX_NOTE_CHARS = 1000
MAX_LIVE = 200  # live entries of one project; retire one before adding past it
MAX_DELTAS = 100  # deltas one review run writes
MAX_SHOWN = 40  # entries the prompt shows at most
REF = re.compile(r"^(?:pb-)?([1-9][0-9]{0,17})$")
# What the text of an entry that holds until a proposal lands says: "until proposal #12 lands", "#12 lands".
UNTIL = re.compile(r"\buntil\s+(?:proposal\s+)?#([1-9][0-9]{0,17})\b|#([1-9][0-9]{0,17})\s+lands\b", re.IGNORECASE)
CURATOR_ACTOR = "curator"  # who a retire of the maintenance is written by, rather than a run's agent
LANDED = ("merged",)  # the states of a proposal's change that retire the entries that hold until it lands
EXPOSURE_LIMIT = 30  # review runs that showed an entry, never found helpful, before the maintenance retires it


class PlaybookProblem(ValueError):
    """A delta that cannot be read; ``str`` says why."""


def ref(entry_id: int) -> str:
    """The id of an entry as the prompt and the command line write it."""
    return f"{PREFIX}{int(entry_id)}"


def parse_ref(text: str) -> int:
    """The entry id of ``pb-12`` or ``12``; PlaybookProblem otherwise."""
    found = REF.match(str(text or "").strip())
    if found is None:
        raise PlaybookProblem(f"an entry of the playbook is pb-N or N, not {text!r}")
    return int(found.group(1))


def clean_text(text: str | None, what: str = "text", limit: int = MAX_TEXT_CHARS) -> str:
    """``text`` on one line, blanks folded; PlaybookProblem when it is empty or longer than ``limit``."""
    line = " ".join(str(text or "").split())
    if not line:
        raise PlaybookProblem(f"the {what} is empty")
    if len(line) > limit:
        raise PlaybookProblem(f"the {what} is at most {limit} characters, not {len(line)}")
    return line


def score(entry: Mapping) -> int:
    return int(entry.get("helpful") or 0) - int(entry.get("harmful") or 0)


def ranked(entries: Iterable[Mapping], lenses: Iterable[str] | None = None) -> list[Mapping]:
    """The live entries of ``lenses`` and those of no lens (every live entry when ``lenses`` is None), the most helpful
    less harmful first, then the most helpful, then the oldest."""
    wanted = None if lenses is None else set(lenses)
    live = [
        entry
        for entry in entries
        if not entry.get("retired_at") and (wanted is None or entry.get("lens") is None or entry.get("lens") in wanted)
    ]
    return sorted(live, key=lambda entry: (-score(entry), -int(entry.get("helpful") or 0), int(entry["id"])))


def line_of(entry: Mapping) -> str:
    """One entry as the prompt shows it: ``- pb-3 (cost, helpful 2, harmful 0): text``."""
    lens = entry.get("lens") or "every lens"
    counts = f"helpful {int(entry.get('helpful') or 0)}, harmful {int(entry.get('harmful') or 0)}"
    return f"- {ref(entry['id'])} ({lens}, {counts}): {entry.get('text')}"


def prompt_lines(entries: Iterable[Mapping], lenses: Iterable[str], most: int = MAX_SHOWN) -> list[str]:
    """The lines of the playbook in the prompt of a review run of ``lenses``."""
    return [line_of(entry) for entry in ranked(entries, lenses)[:most]]


def until_proposal(text: str | None) -> int | None:
    """The proposal the text of an entry says it holds until (``until proposal #12``, ``#12 lands``); None."""
    found = UNTIL.search(str(text or ""))
    return int(found.group(1) or found.group(2)) if found else None


def until_of(entry: Mapping) -> int | None:
    """The proposal ``entry`` holds until: its ``until_proposal_id``, else what its text says."""
    named = entry.get("until_proposal_id")
    if isinstance(named, int) and not isinstance(named, bool) and named > 0:
        return named
    return until_proposal(entry.get("text"))


def due_retires(
    entries: Iterable[Mapping], landed: Mapping[int, str], exposure_limit: int = EXPOSURE_LIMIT
) -> list[tuple[int, str]]:
    """(entry id, note) of each live entry of ``entries`` the maintenance retires: one that holds until a proposal
    whose state in ``landed`` (proposal id -> the state of its change) is one of LANDED, and one shown in at least
    ``exposure_limit`` review runs (``prompt_exposures``) that no run found helpful. Oldest first."""
    due = []
    for entry in sorted(entries, key=lambda item: int(item["id"])):
        if entry.get("retired_at"):
            continue
        until = until_of(entry)
        exposures = int(entry.get("prompt_exposures") or 0)
        if until is not None and landed.get(until) in LANDED:
            due.append((int(entry["id"]), f"proposal #{until} it held until is {landed[until]}"))
        elif exposure_limit > 0 and exposures >= exposure_limit and not int(entry.get("helpful") or 0):
            due.append((int(entry["id"]), f"shown in {exposures} review runs and never found helpful"))
    return due
