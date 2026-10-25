"""What the commands of a run's agent read (``evo_agents.worker.cli``): a text from a file or stdin, within a bound,
an option of a decision and what else a question carries (``decision_extras``), the time and the event a wait wakes on (``wait_until``, ``payload_contains``), the evidence
specs of a finding or a proposal (``evo_agents.hub.review.parse_evidence``) and the code evidence checked against the
run's worktrees (``attest``), and the draft plan or the promotion of a proposal from a YAML or JSON file
(``plan_file``, ``promotion_file``). A problem is a WorkerStateError, which the command prints as one ``error:``
line."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
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


def decision_extras(
    recommended: str | None, reason: str | None, reversible: str | None, undo: str | None, evidence: list[str] | None
) -> dict:
    """What ``worker ask`` sends besides its options (``evo_agents.hub.decision_asks``): ``--recommended-reason``,
    which goes with ``--recommended``; ``--reversible yes|no`` and ``--undo``, which says ``--reversible yes``; and each
    ``--evidence`` as the command line writes the ref. Only what was given; WorkerStateError for what the hub would
    refuse."""
    from evo_agents.hub import decision_asks

    extras: dict = {}
    if reason is not None:
        if recommended is None:
            raise WorkerStateError("--recommended-reason says why you recommend an option: name it with --recommended")
        if not reason.strip() or len(reason) > decision_asks.MAX_REASON_CHARS:
            raise WorkerStateError(f"--recommended-reason takes 1 to {decision_asks.MAX_REASON_CHARS} characters")
        extras["recommended_reason"] = reason
    if undo is not None:
        if reversible == "no":
            raise WorkerStateError("--undo says how to undo the act: it goes with --reversible yes, not no")
        if not undo.strip() or len(undo) > decision_asks.MAX_UNDO_CHARS:
            raise WorkerStateError(f"--undo takes 1 to {decision_asks.MAX_UNDO_CHARS} characters")
        extras["undo"], reversible = undo, "yes"
    if reversible is not None:
        extras["reversible"] = reversible == "yes"
    if len(evidence or ()) > decision_asks.MAX_EVIDENCE:
        raise WorkerStateError(f"a decision names at most {decision_asks.MAX_EVIDENCE} refs with --evidence")
    try:
        refs = [decision_asks.evidence_ref(ref) for ref in evidence or ()]
    except decision_asks.RefProblem as exc:
        raise WorkerStateError(f"--evidence: {exc}") from None
    if refs:
        extras["evidence"] = refs
    return extras


RELATIVE = re.compile(r"^\+(\d{1,5})([mhd])$")  # +90m, +6h, +5d: a wait's time from now
UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def wait_until(text: str, now: datetime) -> str:
    """``--until`` of ``worker wait`` as an ISO 8601 time in UTC: a time with a timezone (``2026-10-30T09:00Z``), or a
    span from ``now`` such as ``+6h``, ``+90m`` or ``+5d``."""
    relative = RELATIVE.fullmatch(text.strip())
    if relative:
        at = now + timedelta(**{UNITS[relative[2]]: int(relative[1])})
    else:
        try:
            at = datetime.fromisoformat(text.strip().replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            at = None
        if at is None or at.utcoffset() is None:
            raise WorkerStateError(
                f"--until takes a time with a timezone, such as 2026-10-30T09:00Z, or a span such as +6h, not {text!r}"
            )
    return at.astimezone(now.tzinfo).isoformat(timespec="seconds")


def payload_contains(texts: list[str]) -> dict:
    """``--contains KEY=VALUE`` of ``worker wait``, each VALUE read as JSON when it is (a number, true, a list), else as
    the text it is, as the object the event's payload must contain."""
    found: dict = {}
    for text in texts:
        key, sep, value = text.partition("=")
        key = key.strip()
        if not sep or not key:
            raise WorkerStateError(f"--contains takes KEY=VALUE, such as canvas=evo-agents, not {text!r}")
        if key in found:
            raise WorkerStateError(f"--contains names {key} twice")
        try:
            found[key] = json.loads(value)
        except ValueError:
            found[key] = value
    return found


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


async def attest(evidence: list[dict], workspaces: dict) -> None:
    """Check each piece of code evidence against the run's worktree of its repo: the file is there, with the line;
    and add the commit the worktree is at. The hub checks the rest."""
    from evo_agents.worker import gitops

    for item in evidence:
        if item["kind"] != "code":
            continue
        workspace = workspaces.get(item["repo"])
        if workspace is None:
            raise WorkerStateError(
                f"code:{item['repo']}:{item['path']}: run works in {', '.join(workspaces) or 'no repo'}, not in "
                f"{item['repo']}"
            )
        path = workspace.worktree / item["path"]
        if not path.resolve().is_relative_to(workspace.worktree.resolve()) or not path.is_file():
            raise WorkerStateError(
                f"code:{item['repo']}:{item['path']}: no such file in the worktree of {item['repo']}"
            )
        if item.get("line") is not None:
            with open(path, "rb") as handle:
                lines = sum(1 for _ in handle)
            if item["line"] > lines:
                raise WorkerStateError(f"code:{item['repo']}:{item['path']}:{item['line']}: the file has {lines} lines")
        head = await gitops.rev(workspace.worktree, "HEAD")
        if head:
            item["commit"] = head


def _mapping_file(source: str, what: str, limit: int, holds: str) -> dict:
    import yaml

    text = read_text(source, what, limit) or ""
    try:
        found = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkerStateError(f"{source} is neither YAML nor JSON: {exc}") from None
    if not isinstance(found, dict):
        raise WorkerStateError(f"{source} holds no {what}: {holds}")
    return json.loads(json.dumps(found, default=str))  # dates as text, as the hub keeps JSON


def plan_file(source: str) -> dict:
    """The draft plan of a proposal (``worker propose --plan-file``)."""
    from evo_agents.hub.review import MAX_DRAFT_BYTES

    return _mapping_file(source, "draft plan", MAX_DRAFT_BYTES, "a draft plan is a mapping with id, goal and steps")


def probe_file(source: str) -> dict:
    """The probe of a proposal (``worker propose --probe-file``): its repo, commit, task and verify, the task and the
    verify masked as a digest is (``evo_agents.hub.redact``) before they leave this machine."""
    from evo_agents.hub.probes import MAX_TASK_BYTES
    from evo_agents.hub.redact import redact

    holds = "a probe is a mapping with repo, task and verify, and commit when the base is not the default branch"
    found = _mapping_file(source, "probe", 2 * MAX_TASK_BYTES, holds)
    for key in ("task", "verify"):
        if isinstance(found.get(key), str):
            found[key] = redact(found[key])
    return found


def promotion_file(source: str) -> dict:
    """The promotion of a proposal of kind memory (``worker propose --promote-file``): its target and its body."""
    from evo_agents.hub.memory import MAX_BODY

    return _mapping_file(source, "promotion", 2 * MAX_BODY, "a promotion is a mapping with target and body")
