"""The proof of each acceptance criterion a step names: one line per criterion when the step is reported done.

A step names criteria of the plan's ``acceptance`` by id in its own ``acceptance`` (``[a4, a9]``); only ``a<number>``
words that a criterion of the plan has as its id count, as ``harness._criteria_issues`` reads them, so a step of an
older plan that holds sentences there, or none, has no criteria and keeps the behaviour it had. Each criterion gets a
proof, given as ``ID=VALUE`` (``evo-agents worker step KEY done --proof``, or ``"proofs"`` of a step run's result
file):

- a test, ``path::name`` (``tests/hub/test_runs.py::test_x``, ``path::Class::test_y``): kind ``test``;
- ``n/a`` and why the criterion does not apply to this step (``n/a: an invariant this step leaves alone``): kind
  ``n/a``;
- anything else on one line is a command that shows it holds: kind ``command``.

The plan's step keeps them as ``proofs``, a list of ``{"id", "kind", "locator"}`` (``{"id", "kind": "n/a",
"reason"}``). A Judge reads the criterion of the plan and only the locator, a path or a command; never the reason,
which the Builder wrote. Standard library only: the worker and the hub both import it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

CRITERION_ID = re.compile(r"a\d+")
KINDS = ("test", "command", "n/a")
NA = "n/a"
MAX_PROOF_CHARS = 2000  # one proof's locator or reason
MAX_PROOFS = 50
CRITERION_CHARS = 600  # the criterion of the plan, as a prompt shows it
# A test: a path with an extension, then ``::`` and the test's name (a class, a method, parameters in brackets).
TEST = re.compile(r"(?P<path>[^\s:]+\.\w+)::(?P<name>[\w.:-]+(?:\[[^\]\s]*\])?)")
FLAG = "--proof"
RESULT_KEY = "proofs"  # the key of a step run's result file


class ProofProblem(ValueError):
    """A proof, or the proofs of a report, that cannot be taken; ``str`` says why."""


def _line(value, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def criteria(plan: Mapping | None, step: Mapping | None) -> list[tuple[str, str]]:
    """``(id, criterion)`` of each criterion of ``plan``'s acceptance that ``step``'s acceptance names, in the step's
    order, once each; empty for a step that names none."""
    named = step.get("acceptance") if isinstance(step, Mapping) else None
    if named is None:
        return []
    words: list[str] = []
    for item in named if isinstance(named, list) else [named]:
        words += CRITERION_ID.findall(str(item)) if isinstance(item, str) else []
    held = plan.get("acceptance") if isinstance(plan, Mapping) else None
    texts = {
        str(item["id"]): item.get("criterion")
        for item in (held if isinstance(held, list) else [])
        if isinstance(item, Mapping) and item.get("id") is not None
    }
    found = []
    for word in dict.fromkeys(w for w in words if CRITERION_ID.fullmatch(w)):
        if word in texts:
            found.append((word, _line(texts[word] or "", CRITERION_CHARS)))
    return found


def parse(given: str) -> dict:
    """The proof ``ID=VALUE`` stands for; ProofProblem when it is not one."""
    key, sep, value = given.partition("=")
    key, value = key.strip(), value.strip()
    if not sep or not CRITERION_ID.fullmatch(key):
        raise ProofProblem(
            f"{FLAG} takes ID=VALUE, the id of a criterion (a4) and its proof: path::test_name, a command, or "
            f"n/a: why; not {_line(given, 80)!r}"
        )
    return proof(key, value)


def proof(key: str, value: str) -> dict:
    """The proof of criterion ``key`` that ``value`` gives; ProofProblem for an empty, multi-line or long one, and for
    ``n/a`` without a reason."""
    if not value or "\n" in value or len(value) > MAX_PROOF_CHARS:
        raise ProofProblem(f"the proof of {key} is one line of 1 to {MAX_PROOF_CHARS} characters")
    if value.lower().startswith(NA):
        reason = value[len(NA) :].lstrip(" :-").strip()
        if not reason:
            raise ProofProblem(f"the proof of {key} is n/a without a reason: give it as {key}=n/a: why")
        return {"id": key, "kind": NA, "reason": reason}
    kind = "test" if TEST.fullmatch(value) else "command"
    return {"id": key, "kind": kind, "locator": value}


def parse_all(given: Iterable[str]) -> list[dict]:
    """The proofs of ``--proof`` given once per criterion; ProofProblem for one given twice."""
    proofs = [parse(item) for item in given]
    _once(proofs)
    return proofs


def from_result(data) -> list[dict] | None:
    """The proofs of a step run's result file (``"proofs"``: an object of criterion id to proof); None when it has
    none."""
    given = data.get(RESULT_KEY) if isinstance(data, Mapping) else None
    if given is None:
        return None
    if not isinstance(given, Mapping) or not all(isinstance(v, str) for v in given.values()):
        raise ProofProblem(f'"{RESULT_KEY}" is an object of criterion id to its proof, each a string')
    proofs = []
    for key, value in given.items():
        if not CRITERION_ID.fullmatch(str(key)):
            raise ProofProblem(f'"{RESULT_KEY}" has {str(key)[:40]!r}, which is not the id of a criterion (a4)')
        proofs.append(proof(str(key), value.strip()))
    return proofs


def _once(proofs: list[dict]) -> None:
    seen: set[str] = set()
    for item in proofs:
        if item["id"] in seen:
            raise ProofProblem(f"{item['id']} has more than one proof: give one line per criterion")
        seen.add(item["id"])
    if len(proofs) > MAX_PROOFS:
        raise ProofProblem(f"at most {MAX_PROOFS} proofs")


def problem(wanted: list[tuple[str, str]], proofs: list[dict]) -> str | None:
    """Why ``proofs`` do not prove the criteria ``wanted`` (``criteria``): one is missing, or one proves a criterion
    the step does not name; None when there is one proof for each."""
    try:
        _once(proofs)
    except ProofProblem as exc:
        return str(exc)
    ids = [key for key, _ in wanted]
    extra = [item["id"] for item in proofs if item["id"] not in ids]
    if not ids and not extra:
        return None
    if not ids:
        return f"the step names no criterion of the plan's acceptance, so it takes no proof (given for {extra[0]})"
    if extra:
        return f"the step names {', '.join(ids)}, not {', '.join(extra)}: give proofs of its own criteria only"
    given = {item["id"] for item in proofs}
    missing = [(key, text) for key, text in wanted if key not in given]
    if not missing:
        return None
    listed = "; ".join(f"{key}: {_line(text, 160)}" if text and text != key else key for key, text in missing)
    return (
        f"each criterion the step names needs one line of proof, and {len(missing)} has none ({listed}); give "
        f"{FLAG} ID=path::test_name, {FLAG} ID=COMMAND or {FLAG} ID=n/a: why for each"
    )


def missing_test(item: Mapping, root) -> str | None:
    """Why the test ``item`` proves with is not in the worktree ``root``: its file is not there (or not under it), or
    holds no test of its name; None for a test that is, and for any other kind."""
    found = TEST.fullmatch(str(item.get("locator") or "")) if item.get("kind") == "test" else None
    if found is None:
        return None
    base = Path(root).resolve()
    path = (base / found["path"]).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return f"{found['path']} is not a file of the repo's worktree"
    name = re.sub(r"\[.*\]$", "", found["name"]).split("::")[-1]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"{found['path']} cannot be read: {exc}"
    if not re.search(rf"\b{re.escape(name)}\b", text):
        return f"{found['path']} has no test named {name}"
    return None


def stored(proofs: Iterable[Mapping]) -> list[dict]:
    """The proofs as the plan's step keeps them: the keys of their kind alone."""
    keep = {"test": ("id", "kind", "locator"), "command": ("id", "kind", "locator"), NA: ("id", "kind", "reason")}
    return [{key: item[key] for key in keep[item["kind"]]} for item in proofs if item.get("kind") in keep]


def locator(item: Mapping) -> str:
    """What a Judge reads of a proof: the test or the command, or n/a without the Builder's reason."""
    kind = item.get("kind")
    if kind == "test":
        return f"test {_line(item.get('locator', ''), 400)}"
    if kind == "command":
        return f"command `{_line(item.get('locator', ''), 400)}`"
    return "n/a (the Builder says it does not apply)"


def judge_lines(plan: Mapping | None, step: Mapping) -> list[str]:
    """The criteria ``step`` names, with the plan's words, each followed by the locator of its proof when the step
    holds one; empty for a step that names none."""
    wanted = criteria(plan, step)
    if not wanted:
        return []
    held = step.get("proofs")
    by_id = {
        str(item.get("id")): item for item in (held if isinstance(held, list) else []) if isinstance(item, Mapping)
    }
    lines = ["Acceptance:"]
    for key, text in wanted:
        lines.append(f"- {key}: {text}")
        if key in by_id:
            lines.append(f"  Proof: {locator(by_id[key])}")
    return lines


def prompt_lines(wanted: list[tuple[str, str]], how: str) -> list[str]:
    """The heading, the criteria and ``how`` the agent reports a proof of each, as a step's prompt shows them."""
    if not wanted:
        return []
    return [
        "## Acceptance criteria of the step",
        *(f"- {key}: {text}" for key, text in wanted),
        how,
    ]


def worker_how() -> str:
    """How the agent of a plan run, or of a step's session, reports the proofs: with ``worker step KEY done``."""
    return (
        f"Report done with one {FLAG} ID=VALUE for each criterion above: the test that proves it (path::test_name), "
        f"the command that shows it, or n/a: why; done without a line for each is refused."
    )


def result_how() -> str:
    """How the agent of a step run reports them: in its result file."""
    return (
        f'Add "{RESULT_KEY}" to the result file: an object with one entry for each criterion above, its id to the test '
        'that proves it ("path::test_name"), the command that shows it, or "n/a: why"; without one for each, the '
        "run fails."
    )
