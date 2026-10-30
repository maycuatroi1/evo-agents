"""The security pass of a Judge (plan super-board-parity-1, step 4): a review for security vulnerabilities that is its
own turn of an agent, apart from the Judge's, for every Judge: the first and the second Judge of the hub's merge queue
and the Curator's Judge of a change. Pure functions over plain values, standard library only, so the hub and the worker
share them (``evo_agents.worker.runner.judge`` runs the turns).

**The text** is the slash command ``security-review`` of anthropics/claude-code-security-review (MIT), copied as it
stands at the commit SOURCE_COMMIT into ``security_review/claude-code-security-review.md``, its license beside it
(``security_review/claude-code-security-review-LICENSE.txt``). The only change is where the diff starts: the command
reads it from ``origin/HEAD``, the pass from the base of the change (``find_prompt``), the merge base the worker
computed. Nothing else of the text is edited; what the pass needs besides (the JSON that ends a reply, a pass that does
not read the Builder) is appended in words of its own.

**The pass** is two kinds of turn, each a new session of the agent on the worktree at the commit judged. One turn finds
(``find_prompt``): the vulnerabilities newly added by the change, as a JSON object. Then one turn for each finding
filters false positives (``filter_prompt``), the "FALSE POSITIVE FILTERING" instructions of the command and the
finding, and answers a confidence from 1 to 10; a finding scored under MIN_CONFIDENCE goes. The pass never reads the
transcript of the Builder: its prompts hold the base, the diff commands and the findings, nothing the Builder wrote.

**The result** (``result``) is the dimension ``security`` of the Judge, as the merge queue's verdict has it, and the
findings in the shape of ``evo_agents.hub.findings``: no finding left is a pass; one left fails the dimension, with
its path and line. A pass that gives no readable findings object fails, so a pass that did not work never passes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from evo_agents.hub import findings as ledger

DIMENSION = "security"
QUESTION = (
    "The diff adds no security vulnerability a person could exploit: a separate review by an agent, which finds them "
    "and then filters out the false positives."
)

SOURCE = "https://github.com/anthropics/claude-code-security-review"
SOURCE_COMMIT = "0c6a49f1fa56a1d472575da86a94dbc1edb78eda"
SOURCE_FILE = ".claude/commands/security-review.md"
SOURCE_SHA256 = "dd1d451f909dbb8798f4a4de010c35e7dd9152f02bb2f7988442cdd138f29e39"
LICENSE_SHA256 = "a9ac868c004c4cce26430b3117767b46397ba4fa25026b1e5ea6694b463baf4b"
FOLDER = Path(__file__).resolve().parent / "security_review"
TEXT_FILE = FOLDER / "claude-code-security-review.md"
LICENSE_FILE = FOLDER / "claude-code-security-review-LICENSE.txt"
DIFF_TARGET = "origin/HEAD"  # where the command starts the diff; the pass starts it at the base of the change

MIN_CONFIDENCE = 8  # a finding scored under it is a false positive
MAX_CANDIDATES = 10  # findings the pass filters; more fail the dimension, unfiltered
SEVERITIES = ("HIGH", "MEDIUM", "LOW")
MAX_MESSAGE = 60_000  # characters of a reply the pass reads
MAX_TRIES = 20  # opening braces of a reply tried for the JSON object that ends it

FILTER_HEADING = "FALSE POSITIVE FILTERING:"
START_HEADING = "START ANALYSIS:"
OUTPUT_HEADING = "REQUIRED OUTPUT FORMAT:"
SEVERITY_HEADING = "SEVERITY GUIDELINES:"

_COMMAND = re.compile(r"^!`(?P<command>[^`]+)`\s*$", re.MULTILINE)


def vendored() -> str:
    """The text of the command, as the file holds it."""
    return TEXT_FILE.read_text(encoding="utf-8")


def vendored_license() -> str:
    return LICENSE_FILE.read_text(encoding="utf-8")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parts(text: str) -> tuple[str, str, str, str]:
    """(the review, the output format, what follows it up to the filtering, the filtering) of the command's text,
    without its front matter and its closing steps."""
    body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL).strip()
    review, _, rest = body.partition(OUTPUT_HEADING)
    output, _, rest = rest.partition(SEVERITY_HEADING)
    guide, _, rest = rest.partition(FILTER_HEADING)
    filtering, _, _ = rest.partition(START_HEADING)
    return review.strip(), output.strip(), (SEVERITY_HEADING + guide).strip(), filtering.strip()


def toward(text: str, base: str) -> str:
    """``text`` with the diff starting at ``base`` where the command had it start at origin/HEAD, and each command the
    slash command would have run for the agent put as one for the agent to run."""
    return _COMMAND.sub(lambda found: "$ " + found.group("command"), text).replace(DIFF_TARGET, base)


FIND_TAIL = """
HOW THIS REVIEW IS RUN HERE:

The commands in the blocks above were not run for you: run them yourself, in the current directory, which holds the
change at the commit to review. You may read any file of it. Read nothing else: you have not been told what the
author meant, and the author's own account of the change is not a source for this review.

Instead of markdown, end your last reply with one JSON object and nothing after it, with at most {limit} findings,
the most severe first, and an empty "findings" list when you found none:

{{"findings": [{{"file": "path/to/file.py", "line": 42, "severity": "HIGH", "category": "sql_injection",
"description": "...", "exploit_scenario": "...", "recommendation": "..."}}]}}
"""

FILTER_HEAD = """You are a senior security engineer. An automated review of a change found the vulnerability below. Decide
whether it is a false positive. The change is checked out in the current directory; its diff starts at {base}.
"""

FILTER_TAIL = """
The finding:

```json
{finding}
```

Reply with one JSON object and nothing after it, "confidence" the score from 1 to 10 of the instructions above:

{{"confidence": 8, "reason": "one sentence"}}
"""


def find_prompt(base: str) -> str:
    """The prompt of the turn that finds: the review of the command with the diff from ``base``, and how its reply
    ends."""
    review, output, guide, _ = _parts(vendored())
    return toward(f"{review}\n\n{guide}\n", base).strip() + "\n" + FIND_TAIL.format(limit=MAX_CANDIDATES)


def filter_prompt(base: str, finding: Mapping) -> str:
    """The prompt of the turn that filters ``finding`` (a candidate as ``candidates`` gives it): the filtering
    instructions of the command, the finding and the score asked."""
    _, _, _, filtering = _parts(vendored())
    shown = {key: finding[key] for key in ("file", "line", "severity", "category", "description", "exploit_scenario")}
    return (
        FILTER_HEAD.format(base=base)
        + "\n"
        + toward(filtering, base)
        + "\n"
        + FILTER_TAIL.format(finding=json.dumps(shown, indent=2))
    )


# Reading a reply


def last_object(message: str | None) -> dict | None:
    """The JSON object that ends ``message`` (a code fence around it is fine, nothing may follow it); None when it
    ends otherwise."""
    text = re.sub(r"\s*`{3,}\s*$", "", (message or "").strip()[-MAX_MESSAGE:])
    if not text.endswith("}"):
        return None
    starts = [found.start() for found in re.finditer(r"\{", text)]
    for start in reversed(starts[-MAX_TRIES:]):
        try:
            data = json.loads(text[start:])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _clean(value, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def candidates(message: str | None) -> list[dict] | None:
    """The findings of the reply that finds, each ``{"file", "line", "severity", "category", "description",
    "exploit_scenario", "recommendation"}``, the most severe first; None when the reply ends with no object holding a
    "findings" list. An item that is no object, or names no file or says nothing, is left out."""
    data = last_object(message)
    if data is None or not isinstance(data.get("findings"), list):
        return None
    found = []
    for item in data["findings"]:
        if not isinstance(item, Mapping):
            continue
        path = _clean(item.get("file"), ledger.MAX_PATH_CHARS)
        description = _clean(item.get("description"), ledger.MAX_TEXT_CHARS)
        if not path or not description:
            continue
        line = item.get("line")
        severity = _clean(item.get("severity"), 10).upper()
        found.append(
            {
                "file": path,
                "line": line if isinstance(line, int) and not isinstance(line, bool) and line >= 1 else None,
                "severity": severity if severity in SEVERITIES else "MEDIUM",
                "category": _clean(item.get("category"), 64) or "security",
                "description": description,
                "exploit_scenario": _clean(item.get("exploit_scenario"), ledger.MAX_TEXT_CHARS),
                "recommendation": _clean(item.get("recommendation"), ledger.MAX_TEXT_CHARS),
            }
        )
    return sorted(found, key=lambda item: SEVERITIES.index(item["severity"]))


def score(message: str | None) -> tuple[int | None, str]:
    """(confidence from 1 to 10, reason) of the reply that filters; (None, "") when the reply ends with no such
    object."""
    data = last_object(message)
    value = None if data is None else data.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 10:
        return None, ""
    return int(value), _clean(data.get("reason"), 300)


# The result


def finding_text(item: Mapping, confidence: int | None) -> str:
    """The text of a finding of the ledger from a candidate and its score."""
    text = f"{item['category']} ({item['severity'].lower()}): {item['description']}"
    if item.get("exploit_scenario"):
        text += f" Exploit: {item['exploit_scenario']}"
    if item.get("recommendation"):
        text += f" Fix: {item['recommendation']}"
    text += f" [confidence {confidence}/10]" if confidence is not None else " [not scored by the filter]"
    return text[: ledger.MAX_TEXT_CHARS]


def result(found: list[dict] | None, scored: Iterable[tuple[dict, int | None]], *, over: int = 0) -> dict:
    """The outcome of the pass: ``{"dimension": {"verdict", "reason"}, "findings": [...]}`` in the shapes the merge
    queue's verdict has (``evo_agents.hub.findings.read_new``).

    ``found`` is what the finding turn gave (None: nothing readable, which fails), ``scored`` each candidate it kept
    for the filter with the confidence the filter gave (None: no readable score, which keeps the finding, the
    conservative side), ``over`` the candidates past MAX_CANDIDATES that no filter saw."""
    if found is None:
        return failed("the security pass gave no findings object: the review cannot be trusted")
    kept, dropped = [], 0
    for item, confidence in scored:
        if confidence is not None and confidence < MIN_CONFIDENCE:
            dropped += 1
            continue
        kept.append(
            {
                "dimension": DIMENSION,
                "path": item["file"],
                "line": item["line"],
                "text": finding_text(item, confidence),
            }
        )
    if over:
        kept.append(
            {
                "dimension": DIMENSION,
                "path": None,
                "line": None,
                "text": f"{over} more candidate(s) than the {MAX_CANDIDATES} the filter reads: they went unfiltered",
            }
        )
    if not kept:
        said = f"{len(found)} candidate(s), {dropped} filtered out as false positives" if found else "none found"
        return {"dimension": {"verdict": "pass", "reason": said}, "findings": []}
    where = ", ".join(_where(item) for item in kept[:3] if item["path"])
    reason = f"{len(kept)} vulnerabilit{'y' if len(kept) == 1 else 'ies'} of confidence {MIN_CONFIDENCE} or more" + (
        f": {where}" if where else ""
    )
    return {"dimension": {"verdict": "fail", "reason": reason}, "findings": kept}


def _where(item: Mapping) -> str:
    return f"{item['path']}:{item['line']}" if item.get("line") else str(item["path"])


def failed(reason: str) -> dict:
    """The result of a pass that could not run or finish: it fails, with ``reason``."""
    return {
        "dimension": {"verdict": "fail", "reason": reason},
        "findings": [{"dimension": DIMENSION, "path": None, "line": None, "text": reason}],
    }
