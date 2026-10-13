"""The handoff of a plan: what a new agent session needs to take a plan up where the sessions before it left it,
built by the hub from the plan it holds (its goal, its steps' status, depends_on and evidence) and nothing else. No
store of its own: ``evo-agents hub plan handoff`` and the MCP tool plan_handoff read GET .../plans/{id}/handoff, which
calls ``build_handoff`` on the plan as the hub holds it, so both answer with the same text.

The text has, in this order: a header naming the plan, its revision and the budget; with a step, the steps that step
depends on, each with its status, the commits its evidence names, its lines that start with "Decision:", verbatim,
and the rest of its evidence, cut; the plan's goal, cut; the other steps done, each the same way; the steps not done,
one line each with its status and depends_on; and, when something had to go, a note naming what was left out and
where to read it whole (plan_show).

The budget is in tokens, estimated as the text's UTF-8 bytes divided by BYTES_PER_TOKEN, rounded up. Three bytes a
token is less than a tokenizer takes for English or Vietnamese prose (kg serve counts 25,000 characters as about 9,000
tokens of Vietnamese), so the text stays within the budget with the tokenizers of today's runtimes. When the whole
text does not fit, the header stays and parts are kept in the order of RANK: the goal, the steps the step depends on
(title, status, commits), the steps not done, the Decision lines of the steps it depends on, the other steps done,
their Decision lines, then the rest of the evidence, each kind in plan order. A part that does not fit is left out
whole, so a Decision line is never cut, and the note at the end names what was left out. Text cut to a part's own
limit (the goal, the rest of an evidence) ends with CUT_MARK, which says how much was left out.

The same plan at the same revision, for the same step and budget, gives the same text, byte for byte. Standard
library only, besides ``evo_agents.hub.plans``: the server, the client and the worker may all import it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from evo_agents.hub.plans import step_index, step_key

BYTES_PER_TOKEN = 3
DEFAULT_BUDGET_TOKENS = 4000  # 12,000 bytes: within a step prompt's MAX_PROMPT_BYTES with the step itself
MIN_BUDGET_TOKENS = 1000  # 3,000 bytes: the header and the longest note fit with room to spare
MAX_BUDGET_TOKENS = 8000  # 24,000 bytes: under kg serve's CAP_CHARS, so the MCP answer is never cut by kg_more
GOAL_BYTES = 1536  # the goal, or a quarter of the budget when that is less
EVIDENCE_BYTES = 1024  # the rest of the evidence of a step done, after its Decision lines
DEPENDENCY_EVIDENCE_BYTES = 2048  # and of a step the handoff's step depends on
TITLE_BYTES = 120
KEY_BYTES = 60  # a step key in a line
DEPENDS_BYTES = 120  # the depends_on of a step not done
MAX_COMMITS = 12  # commits named for one step; its evidence has the others
NOTE_GROUP_BYTES = 160  # the step keys the note names for one kind of part, before "and N more"
NOTE_RESERVE = 400  # bytes kept free for the note while parts are chosen; a longer note takes the last ones out
NOTE_SLACK = 64  # bytes freed beyond the overrun, as the note grows with each step it names
DECISION = "Decision:"
CUT_MARK = " [cut: {omitted} of {total} bytes left out; {where} has it whole]"
# A commit hash in an evidence: 7 to 40 hex digits on their own, with a digit and a letter, or after repo@.
COMMIT = re.compile(r"(?<![0-9A-Za-z_-])[0-9a-f]{7,40}(?![0-9A-Za-z_-])")

# What is kept first when the budget is short (the lower, the earlier).
RANK = {
    ("goal", "goal"): 1,
    ("deps", "entry"): 2,
    ("open", "line"): 3,
    ("deps", "decision"): 4,
    ("done", "entry"): 5,
    ("done", "decision"): 6,
    ("deps", "evidence"): 7,
    ("done", "evidence"): 8,
}
SECTIONS = ("deps", "goal", "done", "open")  # in the order of the text


@dataclass(frozen=True)
class Handoff:
    text: str
    tokens: int  # estimated: UTF-8 bytes / BYTES_PER_TOKEN, rounded up
    cut: bool  # whether anything was cut or left out to stay within the budget


@dataclass(frozen=True)
class _Part:
    section: str
    kind: str  # goal, entry, decision, evidence or line
    order: int  # place in its section
    text: str  # one or more lines, without the last newline
    step: str | None = None  # the key of the step it is about
    owner: int | None = None  # the order of the entry it belongs to: kept only with it

    @property
    def rank(self) -> tuple[int, str, int]:
        return RANK[(self.section, self.kind)], self.section, self.order

    @property
    def id(self) -> tuple[str, int]:
        return self.section, self.order

    @property
    def size(self) -> int:
        return len(self.text.encode()) + 1


def estimate_tokens(text: str) -> int:
    """The tokens ``text`` takes as the handoff counts them: its UTF-8 bytes / BYTES_PER_TOKEN, rounded up."""
    return math.ceil(len(text.encode()) / BYTES_PER_TOKEN)


def check_budget(budget_tokens: int) -> None:
    if not MIN_BUDGET_TOKENS <= budget_tokens <= MAX_BUDGET_TOKENS:
        raise ValueError(
            f"a handoff's budget is {MIN_BUDGET_TOKENS} to {MAX_BUDGET_TOKENS} tokens, not {budget_tokens}"
        )


# Reading a step


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


def _one_line(value, limit: int, missing: str) -> str:
    """``value`` as one line of at most ``limit`` UTF-8 bytes, a longer one ending with "..."."""
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in _text(value)[: 4 * limit]).split())
    if not text:
        return missing
    data = text.encode()
    if len(data) <= limit and len(_text(value)) <= 4 * limit:
        return text
    return data[: max(0, limit - 3)].decode("utf-8", "ignore").rstrip() + "..."


def clip(text: str, limit: int, where: str) -> tuple[str, bool]:
    """``text`` in at most ``limit`` UTF-8 bytes, and whether it was cut: its start, cut on a character boundary,
    and CUT_MARK naming how much was left out and ``where`` to read it whole."""
    data = text.encode()
    if len(data) <= limit:
        return text, False
    longest = CUT_MARK.format(omitted=len(data), total=len(data), where=where).encode()
    head = data[: max(0, limit - len(longest))].decode("utf-8", "ignore").rstrip()
    return head + CUT_MARK.format(omitted=len(data) - len(head.encode()), total=len(data), where=where), True


def decisions_of(evidence: str) -> list[str]:
    """The lines of ``evidence`` that start with "Decision:", verbatim but for the blanks around them."""
    return [line.strip() for line in evidence.splitlines() if line.lstrip().startswith(DECISION)]


def rest_of(evidence: str) -> str:
    """``evidence`` without its Decision lines."""
    return "\n".join(line for line in evidence.splitlines() if not line.lstrip().startswith(DECISION)).strip()


def commits_of(evidence: str) -> list[str]:
    """The commit hashes ``evidence`` names, in order, each once: 7 to 40 hex digits holding a digit and a letter, or
    any after an @ (repo@sha)."""
    found: list[str] = []
    for match in COMMIT.finditer(evidence):
        sha = match.group()
        after_at = evidence[match.start() - 1 : match.start()] == "@"
        if (after_at or (not sha.isdigit() and any(ch.isdigit() for ch in sha))) and sha not in found:
            found.append(sha)
    return found


def _status(step) -> str:
    status = step.get("status") if isinstance(step, dict) else None
    return "pending" if status is None else _one_line(status, KEY_BYTES, "pending")


# The parts


@dataclass
class _Builder:
    """The parts of a handoff, in the order of the text within each section."""

    plan: dict
    budget_bytes: int
    parts: list[_Part] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    cut: bool = False

    def add(self, section: str, kind: str, text: str, step: str | None = None, owner: int | None = None) -> int:
        order = self.counts.get(section, 0)
        self.counts[section] = order + 1
        self.parts.append(_Part(section, kind, order, text, step, owner))
        return order

    def clipped(self, text: str, limit: int, where: str) -> str:
        text, cut = clip(text, limit, where)
        self.cut |= cut
        return text

    def step_parts(self, section: str, key: str, step, *, status: bool, evidence_bytes: int) -> None:
        """A step's entry (title, status when ``status``, commits), its Decision lines and the rest of its evidence."""
        title = _one_line(step.get("title"), TITLE_BYTES, "untitled")
        evidence = _text(step.get("evidence"))
        lines = [f"### Step {key}: {title}" + (f" ({_status(step)})" if status else "")]
        commits = commits_of(evidence)
        if commits:
            more = f" and {len(commits) - MAX_COMMITS} more" if len(commits) > MAX_COMMITS else ""
            lines.append(f"Commits: {', '.join(commits[:MAX_COMMITS])}{more}")
        if not evidence:
            lines.append("No evidence recorded.")
        entry = self.add(section, "entry", "\n".join(lines), key)
        for line in decisions_of(evidence):
            self.add(section, "decision", line, key, entry)
        rest = rest_of(evidence)
        if rest:
            where = f"plan_show with step {key}"
            self.add(section, "evidence", "Evidence: " + self.clipped(rest, evidence_bytes, where), key, entry)

    def dependencies(self, step) -> list[str]:
        depends_on = step.get("depends_on") if isinstance(step, dict) else None
        if not isinstance(depends_on, list):
            return []
        by_key = {}
        for index, other in enumerate(_steps(self.plan)):
            by_key.setdefault(step_key(other, index), other)
        keys = list(dict.fromkeys(str(dep) for dep in depends_on))
        for dep in keys:
            key = _one_line(dep, KEY_BYTES, "?")
            other = by_key.get(dep)
            if isinstance(other, dict):
                self.step_parts("deps", key, other, status=True, evidence_bytes=DEPENDENCY_EVIDENCE_BYTES)
            else:
                self.add("deps", "entry", f"### Step {key}: the plan has no such step", key)
        return keys

    def goal(self) -> None:
        goal = _text(self.plan.get("goal"))
        if goal:
            self.add("goal", "goal", self.clipped(goal, min(GOAL_BYTES, self.budget_bytes // 4), "plan_show"))

    def done(self, skip: list[str]) -> None:
        for index, step in enumerate(_steps(self.plan)):
            key = step_key(step, index)
            if isinstance(step, dict) and step.get("status") == "done" and key not in skip:
                key = _one_line(key, KEY_BYTES, "?")
                self.step_parts("done", key, step, status=False, evidence_bytes=EVIDENCE_BYTES)

    def open(self) -> int:
        count = 0
        for index, step in enumerate(_steps(self.plan)):
            if isinstance(step, dict) and step.get("status") == "done":
                continue
            count += 1
            key = _one_line(step_key(step, index), KEY_BYTES, "?")
            if not isinstance(step, dict):
                self.add("open", "line", f"- {key} [pending] {_one_line(step, TITLE_BYTES, 'untitled')}", key)
                continue
            line = f"- {key} [{_status(step)}] {_one_line(step.get('title'), TITLE_BYTES, 'untitled')}"
            depends_on = step.get("depends_on")
            if isinstance(depends_on, list) and depends_on:
                line += " (after " + _one_line(", ".join(str(dep) for dep in depends_on), DEPENDS_BYTES, "?") + ")"
            self.add("open", "line", line, key)
        return count


def _steps(plan: dict) -> list:
    steps = plan.get("steps")
    return steps if isinstance(steps, list) else []


# Fitting them in the budget


def _headings(plan: dict, key: str | None, open_count: int) -> dict[str, str]:
    return {
        "deps": f"## The steps step {key} depends on",
        "goal": "## Goal",
        "done": "## Other steps done" if key is not None else "## Steps done",
        "open": f"## Steps not done: {open_count} of {len(_steps(plan))}",
    }


def _render(header: str, headings: dict[str, str], parts: list[_Part], kept: set, note: str | None) -> str:
    lines = [header]
    for section in SECTIONS:
        chosen = [part for part in parts if part.section == section and part.id in kept]
        if chosen:
            lines += ["", headings[section], *(part.text for part in chosen)]
    if note:
        lines += ["", note]
    return "\n".join(lines) + "\n"


def _listed(label: str, keys: list[str]) -> str:
    """``label`` and as many of ``keys`` as NOTE_GROUP_BYTES holds, then how many more there are."""
    shown: list[str] = []
    for key in keys:
        if shown and len(", ".join([*shown, key]).encode()) > NOTE_GROUP_BYTES:
            break
        shown.append(key)
    more = f" and {len(keys) - len(shown)} more" if len(keys) > len(shown) else ""
    return f"{label} {', '.join(shown)}{more}"


def _note(parts: list[_Part], kept: set, budget_tokens: int, plan_id: str, project: str) -> str | None:
    """What was left out of ``parts``, by kind, and where to read it whole; None when nothing was."""
    left = [part for part in parts if part.id not in kept]
    if not left:
        return None
    gone = {part.id for part in left if part.kind == "entry"}

    def keys(section: str, kind: str) -> list[str]:
        found = [p.step for p in left if (p.section, p.kind) == (section, kind) and (section, p.owner) not in gone]
        return list(dict.fromkeys(found))

    groups = ["the goal"] if any(part.kind == "goal" for part in left) else []
    for label, found in (
        ("the steps it depends on:", keys("deps", "entry")),
        ("steps not done:", keys("open", "line")),
        ("steps done:", keys("done", "entry")),
        ("Decision lines of steps", list(dict.fromkeys(keys("deps", "decision") + keys("done", "decision")))),
        ("the rest of the evidence of steps", list(dict.fromkeys(keys("deps", "evidence") + keys("done", "evidence")))),
    ):
        if found:
            groups.append(_listed(label, found))
    return (
        f"[Left out to stay within {budget_tokens} tokens: {'; '.join(groups)}. plan_show (MCP evo-hub, plan_id "
        f"{plan_id}, with step for one step) or `evo-agents hub plan show {plan_id} --project {project}` has them.]"
    )


def _fit(header: str, headings: dict, parts: list[_Part], budget: int, note_of) -> tuple[set, str | None]:
    """The ids of the parts kept and the note: every part when the whole text fits in ``budget`` bytes, else as many
    as fit in the order of RANK, each whole, with room for the note. A part goes only with the entry it belongs to."""
    if len(_render(header, headings, parts, {part.id for part in parts}, None).encode()) <= budget:
        return {part.id for part in parts}, None
    chosen: list[_Part] = []  # in the order of RANK, so the last one is the first to go
    kept: set = set()
    sections: dict[str, int] = {}  # parts kept per section, whose heading costs once
    used = len(header.encode()) + 1

    def cost(part: _Part) -> int:
        return part.size + (0 if sections.get(part.section) else len(headings[part.section].encode()) + 2)

    for part in sorted(parts, key=lambda part: part.rank):
        if part.owner is not None and (part.section, part.owner) not in kept:
            continue
        if used + cost(part) <= budget - NOTE_RESERVE:
            used += cost(part)
            chosen.append(part)
            kept.add(part.id)
            sections[part.section] = sections.get(part.section, 0) + 1
    while True:  # a note longer than its reserve: the parts kept last go until it fits
        note = note_of(kept)
        over = used + len(note.encode()) + 2 - budget
        if over <= 0 or not chosen:
            return kept, note
        while over > -NOTE_SLACK and chosen:
            part = chosen.pop()
            kept.discard(part.id)
            sections[part.section] -= 1
            freed = cost(part)
            used -= freed
            over -= freed


def build_handoff(
    plan: dict, *, project: str, revision: int, step=None, budget_tokens: int = DEFAULT_BUDGET_TOKENS
) -> Handoff:
    """The handoff of ``plan`` (a plan body as the hub holds it) at ``revision`` of project ``project``, in at most
    ``budget_tokens`` tokens; with ``step``, for that step, the evidence of the steps it depends on first. Raises
    PlanProblem when ``step`` names no step of the plan, and ValueError for a budget outside MIN_BUDGET_TOKENS to
    MAX_BUDGET_TOKENS."""
    check_budget(budget_tokens)
    plan_id, project = _one_line(plan.get("id"), 100, "without an id"), _one_line(project, 100, "?")
    budget = budget_tokens * BYTES_PER_TOKEN
    builder = _Builder(plan, budget)
    key, skip = None, []
    if step is not None:
        target = _steps(plan)[step_index(plan, step)]
        key = _one_line(step, KEY_BYTES, "?")
        skip = builder.dependencies(target)
    builder.goal()
    builder.done(skip)
    headings = _headings(plan, key, builder.open())
    header = (
        f"# Handoff of plan {plan_id}, revision {revision}, project {project}"
        + (f"; for step {key}" if key is not None else "")
        + f"\nBuilt by the hub from the plan it holds, in at most {budget_tokens} tokens. plan_show (MCP evo-hub) or "
        f"`evo-agents hub plan show {plan_id} --project {project}` has the whole plan."
    )
    parts = builder.parts
    kept, note = _fit(header, headings, parts, budget, lambda ids: _note(parts, ids, budget_tokens, plan_id, project))
    text = _render(header, headings, parts, kept, note)
    if len(text.encode()) > budget:  # a header and note longer than the budget, which MIN_BUDGET_TOKENS rules out
        text = text.encode()[:budget].decode("utf-8", "ignore")
    return Handoff(text, estimate_tokens(text), builder.cut or note is not None)
