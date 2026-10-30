"""What a Judge found wrong in a change, remembered across its rounds (plan super-board-parity-1, step 3): each finding
has an id that stays the same for as long as the change lives, its dimension, its path and line, and its text. Pure
functions over plain values, standard library only, so the hub's merge queue (``evo_agents.hub.merge_gate``), the
worker and, later, the Curator's Judge and the Judge of a GitHub pull request share them.

**A round.** A Judge ends its verdict with ``findings``, what it finds wrong in the change at the head it judges, one
problem each, with the file and line where it is (``read_new``), and ``prior``, a mark for each finding of an earlier
round its prompt listed (``prompt_lines``): ``fixed``, ``not_fixed`` or ``no_longer_applies``, with the file:line that
shows it (``read_marks``). The hub does not take the Judge's word for it (``check``): an open finding it left
unmarked, marked ``not_fixed``, or marked without a file:line fails the finding's dimension, and so does a new finding
on a dimension the Judge passed.

**The ledger** (``record_round``) is the list of every finding of the change, the oldest first, each a dict: ``id``
(``F1``, ``F2``, and on), ``dimension``, ``path`` and ``line`` (None when it names none), ``text``, ``status``
(``open``, ``fixed`` or ``no_longer_applies``), ``run_id`` and ``head_sha`` of the round that raised it, ``marks``
(what each later round said of it), and ``posted`` once a comment named it. A dimension a Judge failed with no finding
of its own gets one, of the dimension's reason, so the next round has it to check. A ledger read from a verdict kept
before findings existed is empty: nothing to check.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

OPEN, FIXED, NOT_FIXED, GONE = "open", "fixed", "not_fixed", "no_longer_applies"
MARKS = (FIXED, NOT_FIXED, GONE)
CLOSED = (FIXED, GONE)
MAX_NEW = 50  # findings a round raises
MAX_LEDGER = 200  # findings a change keeps; the oldest closed ones go first
MAX_MARKS = 10  # marks a finding keeps, the newest
MAX_TEXT_CHARS = 1000
MAX_PATH_CHARS = 500  # tiers.MAX_PATH_CHARS
MAX_WHERE_CHARS = MAX_PATH_CHARS + 20
MAX_DIMENSION_CHARS = 64
ID = re.compile(r"F[1-9][0-9]{0,4}")


def _text(value, limit: int = MAX_TEXT_CHARS) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _line(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def read_new(raw, names: Iterable[str] | None = None) -> list[dict]:
    """The new findings a Judge gave (``raw``, its ``findings``), each ``{"dimension", "path", "line", "text"}``; one
    without a text, or of a dimension not among ``names`` (when given), is left out, and at most MAX_NEW stay."""
    wanted = None if names is None else set(names)
    found = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        dimension, text = _text(item.get("dimension"), MAX_DIMENSION_CHARS), _text(item.get("text"))
        if not dimension or not text or (wanted is not None and dimension not in wanted):
            continue
        path = _text(item.get("path"), MAX_PATH_CHARS) or None
        found.append(
            {"dimension": dimension, "path": path, "line": _line(item.get("line")) if path else None, "text": text}
        )
        if len(found) == MAX_NEW:
            break
    return found


def read_marks(raw) -> dict[str, dict]:
    """The marks a Judge gave the findings of earlier rounds (``raw``, its ``prior``): id -> ``{"status", "where",
    "reason"}``; one of another status, or of no finding id, is left out."""
    found: dict[str, dict] = {}
    for key, value in (raw if isinstance(raw, Mapping) else {}).items():
        if not isinstance(key, str) or not ID.fullmatch(key) or not isinstance(value, Mapping):
            continue
        if value.get("status") not in MARKS:
            continue
        where = _text(value.get("where"), MAX_WHERE_CHARS) or None
        found[key] = {"status": value["status"], "where": where, "reason": _text(value.get("reason"))}
        if len(found) == MAX_LEDGER:
            break
    return found


def ledger_of(verdict: Mapping | None) -> list[dict]:
    """The ledger a verdict keeps under ``findings``; empty for a verdict kept before findings existed."""
    found = verdict.get("findings") if isinstance(verdict, Mapping) else None
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict) and ID.fullmatch(str(item.get("id")))]


def open_findings(ledger: Iterable[Mapping], names: Iterable[str]) -> list[dict]:
    """The findings of ``ledger`` still open on one of ``names``, the dimensions a Judge judges."""
    wanted = set(names)
    return [dict(item) for item in ledger if item.get("status") == OPEN and item.get("dimension") in wanted]


def where(finding: Mapping) -> str:
    """``path:line``, ``path``, or ``(no file)`` of a finding."""
    path = finding.get("path")
    if not path:
        return "(no file)"
    return f"{path}:{finding['line']}" if finding.get("line") else str(path)


def label(finding: Mapping) -> str:
    """A finding on one line: its id, dimension, file:line and text."""
    return f"{finding['id']} ({finding.get('dimension')}, {where(finding)}): {finding.get('text')}"


def prompt_lines(open_ones: Iterable[Mapping]) -> list[str]:
    """The part of a Judge's prompt that lists the findings of earlier rounds still open, and asks for a mark of each
    before it reviews the change anew; empty when there is none."""
    items = list(open_ones)
    if not items:
        return []
    return [
        "# Findings of earlier rounds",
        "Earlier rounds of this change found what follows. Before you review the change anew, check each one at the "
        'commit you judge and mark it in "prior": fixed, not_fixed or no_longer_applies, with the file:line that shows '
        'it ("where"). A finding you mark not_fixed, or leave unmarked, fails its dimension whatever else you say; '
        "do not raise it again as a new finding.",
        *(f"- {label(item)}" for item in items),
    ]


def verdict_shape() -> str:
    """How a Judge writes its findings and marks in its verdict object."""
    return (
        '"findings": [{"dimension": "...", "path": "path/of/the/file", "line": 12, "text": "what is wrong"}], '
        '"prior": {"F1": {"status": "fixed" or "not_fixed" or "no_longer_applies", "where": "path:line", '
        '"reason": "..."}}'
    )


def verdict_rule() -> str:
    """The rule of a Judge's prompt on its findings."""
    return (
        '- In "findings", name each thing you find wrong, one problem a finding, with the dimension it fails and the '
        "file and line where it is; a dimension you fail has at least one, and a dimension you pass has none. "
        'In "prior", mark every finding of earlier rounds the prompt lists; give an empty object when it lists none.'
    )


def check(
    open_ones: Iterable[Mapping], marks: Mapping[str, Mapping], new: Iterable[Mapping], dimensions: Mapping
) -> dict[str, list[str]]:
    """What a round fails because of its findings, as the hub reads it and not as the Judge says: dimension -> why.
    Each finding of ``open_ones`` left unmarked, marked not_fixed, or marked fixed or no longer applies without a
    file:line fails its dimension; so does a new finding on a dimension ``dimensions`` has passed."""
    failed: dict[str, list[str]] = {}
    for item in open_ones:
        mark = marks.get(item["id"])
        if mark is None:
            why = f"the Judge did not say whether {item['id']} is fixed"
        elif mark["status"] == NOT_FIXED:
            why = f"{item['id']} is not fixed" + (f": {mark['reason']}" if mark.get("reason") else "")
        elif not mark.get("where"):
            why = f"the Judge marked {item['id']} {mark['status']} without the file:line that shows it"
        else:
            continue
        failed.setdefault(item["dimension"], []).append(why)
    for item in new:
        given = dimensions.get(item["dimension"])
        if isinstance(given, Mapping) and given.get("verdict") == "pass":
            why = f"the Judge raised a finding on {item['dimension']} and passed it: {where(item)}"
            failed.setdefault(item["dimension"], []).append(why)
    return failed


def supersede(ledger: Iterable[Mapping], dimension: str, *, run_id: int | None, head: str | None) -> list[dict]:
    """The ledger with each finding still open on ``dimension`` closed as no longer applying: the security pass reads
    the whole change again at every head and raises what it still sees as new findings, so it marks nothing of
    earlier rounds."""
    kept = [dict(item) for item in ledger]
    for item in kept:
        if item.get("status") == OPEN and item.get("dimension") == dimension:
            line = {
                "status": GONE,
                "where": None,
                "reason": f"the {dimension} pass ran again; a finding it still sees is raised again",
                "run_id": run_id,
                "head_sha": head,
            }
            item["marks"] = [*(item.get("marks") or []), line][-MAX_MARKS:]
            item["status"] = GONE
    return kept


def next_id(ledger: Iterable[Mapping]) -> int:
    """The number of the next finding of a change."""
    return 1 + max((int(str(item["id"])[1:]) for item in ledger), default=0)


def record_round(
    ledger: Iterable[Mapping],
    *,
    names: Iterable[str],
    marks: Mapping[str, Mapping],
    new: Iterable[Mapping],
    dimensions: Mapping,
    run_id: int | None,
    head: str | None,
) -> tuple[list[dict], list[dict]]:
    """(the ledger after a round of a Judge of ``names``, the findings the round raised, with their ids). The marks
    move the open findings of ``names``: fixed and no longer applies close them, not_fixed keeps them open; each new
    finding gets the next id, and a dimension failed with no finding of its own, new or still open, gets one of its
    reason."""
    kept = [dict(item) for item in ledger]
    judged = set(names)
    still_open: set[str] = set()
    for item in kept:
        if item.get("status") != OPEN or item.get("dimension") not in judged:
            continue
        mark = marks.get(item["id"])
        if mark is not None:
            line = {
                "status": mark["status"],
                "where": mark.get("where"),
                "reason": mark.get("reason") or "",
                "run_id": run_id,
                "head_sha": head,
            }
            item["marks"] = [*(item.get("marks") or []), line][-MAX_MARKS:]
            if mark["status"] in CLOSED and mark.get("where"):
                item["status"] = mark["status"]
        if item["status"] == OPEN:
            still_open.add(item["dimension"])
    raised = [dict(item) for item in new]
    covered = still_open | {item["dimension"] for item in raised}
    for name in names:
        given = dimensions.get(name)
        if isinstance(given, Mapping) and given.get("verdict") == "fail" and name not in covered:
            reason = _text(given.get("reason")) or f"the Judge failed {name}"
            raised.append({"dimension": name, "path": None, "line": None, "text": reason})
    number = next_id(kept)
    for offset, item in enumerate(raised):
        item.update(id=f"F{number + offset}", status=OPEN, run_id=run_id, head_sha=head, marks=[], posted=False)
    kept += raised
    while len(kept) > MAX_LEDGER:  # the oldest closed finding goes first, else the oldest
        index = next((i for i, item in enumerate(kept) if item.get("status") != OPEN), 0)
        kept.pop(index)
    return kept, raised


def carried(verdict: Mapping | None) -> dict | None:
    """What a verdict hands the rounds after it, once its patch changed: the ledger alone; None without findings."""
    ledger = ledger_of(verdict)
    return {"findings": ledger} if ledger else None


def unposted(ledger: Iterable[Mapping]) -> tuple[list[dict], list[dict]]:
    """(the open findings no comment named yet, those a comment named)."""
    found = [dict(item) for item in ledger if item.get("status") == OPEN]
    return [item for item in found if not item.get("posted")], [item for item in found if item.get("posted")]


def mark_posted(ledger: Iterable[Mapping], ids: Iterable[str]) -> list[dict]:
    """``ledger`` with the findings ``ids`` named by a comment."""
    named = set(ids)
    return [{**item, "posted": True} if item.get("id") in named else dict(item) for item in ledger]
