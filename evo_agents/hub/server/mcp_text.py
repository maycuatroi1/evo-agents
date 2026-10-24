"""The text the hub's MCP tools ``digest_show`` and ``run_events`` give the model (``mcp.HubMcp``), kept under
TEXT_CHARS characters, the lines that name a memory in what ``memory_search``, ``memory_get`` and ``memory_write``
give it, and the lines of a problem in what ``problem_list`` and ``problem_show`` give it (``problem_line``,
``problem_lines``).

The envelope of ``kg serve`` cuts a text over CAP_CHARS and gives the rest through kg_more, and the spool hook of the
evo-hub plugin leaves the hub's tools whole, so these two keep under the cut themselves: the Curator's review reads
them whole. ``digest_show`` leaves out the last items of a digest's longest lists (``fit_digest``) and ``run_events``
stops before the event whose line would pass it (``event_lines``).
"""

from __future__ import annotations

import json

from evo_agents.hub import problems
from evo_agents.hub.memory import FRONTMATTER, frontmatter
from evo_agents.kg.serve import CAP_CHARS

# Characters of the text of digest_show and run_events, under the envelope's cut: the Curator's review reads them
# whole, without kg_more, and the spool hook leaves the tools of evo-hub whole.
TEXT_CHARS = CAP_CHARS - 1_000
EVENT_CHARS = 500  # characters of one event's line in run_events
EVENT_ROOM = 200  # characters run_events keeps for its first and last lines
EXCERPT = 200  # characters of a memory shown by memory_search


def one_line(text: str, limit: int) -> str:
    """``text`` on one line, its runs of white space made one space, cut to ``limit`` characters with ``...``."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def memory_where(memory) -> str:
    if memory.scope == "project":
        agent = f", memory of agent @{memory.agent}" if getattr(memory, "agent", None) else ""
        return f"project {memory.project}, {memory.location}{agent}"
    return f"personal, {memory.location}"


def memory_line(memory) -> str:
    deleted = ", deleted" if memory.deleted else ""
    return f"[{memory.id}] {memory.name}: {memory.type}, {memory_where(memory)}, revision {memory.revision}{deleted}"


def memory_about(body: str) -> str:
    """The description of a memory's frontmatter, else the start of its text."""
    described = frontmatter(body).get("description")
    if isinstance(described, str) and described.strip():
        return one_line(described, EXCERPT)
    found = FRONTMATTER.match(body)
    return one_line(body[found.end() :] if found else body, EXCERPT)


def digest_json(body: dict) -> str:
    """A digest as JSON, one key on each line, its value on one line."""
    fields = (f" {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}" for key, value in body.items())
    return "{\n" + ",\n".join(fields) + "\n}"


def fit_digest(body: dict, budget: int) -> tuple[dict, dict[str, int]]:
    """``body`` with the last items of its longest lists left out until ``digest_json`` of it fits in ``budget``
    characters, and how many items each list lost. A kept item keeps its index, which session:ID:FIELD:INDEX
    evidence names; the lists put the most called tools, the most repeated commands and errors and the earliest
    user_turns first (``evo_agents.hub.digest``), so their last items go."""
    kept = dict(body)
    while (excess := len(digest_json(kept)) - budget) > 0:
        sizes = sorted(
            (
                (len(json.dumps(value, ensure_ascii=False)), key)
                for key, value in kept.items()
                if isinstance(value, list)
            ),
            reverse=True,
        )
        if not sizes or sizes[0][0] <= 2:  # nothing left to take off
            break
        (size, key), floor = sizes[0], (sizes[1][0] if len(sizes) > 1 else 0)
        items = list(kept[key])
        while items and excess > 0:
            taken = len(json.dumps(items.pop(), ensure_ascii=False)) + 2
            excess, size = excess - taken, size - taken
            if size <= floor:  # another list is the longest now
                break
        kept[key] = items
    left_out = {key: len(value) - len(kept[key]) for key, value in body.items() if isinstance(value, list)}
    return kept, {key: n for key, n in left_out.items() if n}


def event_text(body: dict) -> str:
    """What an event says, for one line: its text, the text of its content, else its JSON."""
    if isinstance(body.get("text"), str):
        return body["text"]
    content = body.get("content")
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return json.dumps(body, ensure_ascii=False, default=str)


def event_lines(events) -> list[str]:
    """One line for each of the first ``events`` (with ``seq``, ``at``, ``kind`` and ``body``) whose lines fit in
    TEXT_CHARS, less EVENT_ROOM for the first and last lines of the text; the first event's line always."""
    lines, used = [], 0
    for event in events:
        line = f"{event.seq} {event.at:%H:%M:%S} {event.kind}: {one_line(event_text(event.body), EVENT_CHARS)}"
        if lines and used + len(line) + 1 > TEXT_CHARS - EVENT_ROOM:
            break
        lines.append(line)
        used += len(line) + 1
    return lines


def problem_line(problem: dict) -> str:
    """A problem of ``problem_list`` in one line: its code, status, rank, owner, metric and statement."""
    return (
        f"{problem['code']} ({problems.status_text(problem)}, rank {problem['rank']}, owner {problem['owner']}, "
        f"{problems.metric_text(problem['metric'])}): {one_line(problem['statement'], EXCERPT)}"
    )


def _listed(title: str, none: str, lines: list[str]) -> list[str]:
    return [title, *lines] if lines else [none]


def problem_lines(project: str, problem: dict) -> list[str]:
    """What ``problem_show`` says of a problem (its JSON, as ProjectProblemShown has it): its fields, then the goals
    that answer it, the claims that link it and the plans that name it."""
    goals = [
        f"- {goal['code']} ({goal['status']}): {one_line(goal['statement'], EXCERPT)}" for goal in problem["goals"]
    ]
    claims = [
        f"- {claim['code']} in canvas {claim['canvas']}, cell {claim['cell']} ({claim['status']}): "
        + one_line(claim["text"], EXCERPT)
        for claim in problem["claims"]
    ]
    plans = [
        f"- {plan['plan_id']} ({plan['area']}, {plan['steps_done']}/{plan['steps_total']} steps done)"
        + (f": {one_line(plan['title'], EXCERPT)}" if plan["title"] else "")
        for plan in problem["plans"]
    ]
    return [
        f"# Problem {problem['code']} of project {project}, revision {problem['revision']}",
        f"statement: {problem['statement']}",
        f"who: {problem['who']}",
        f"when: {problem['when']}",
        f"workaround: {problem['workaround']}",
        f"metric: {problems.metric_text(problem['metric'])}",
        f"rank: {problem['rank']}; owner: {problem['owner']}; status: {problems.status_text(problem)}",
        *_listed("goals that answer it:", "no goal answers it yet", goals),
        *_listed("claims that link it:", "no claim links it yet", claims),
        *_listed("plans that name it:", "no plan names it yet", plans),
    ]


def evidence_lines(code: str, project: str, lines: list[dict]) -> list[str]:
    """What ``problem_evidence`` says of a problem's evidence (its lines as ProblemEvidence has them): a header with the
    count of lines and of independent sources, then a line each."""
    sources = problems.sources_of(lines)
    head = f"# Evidence of problem {code} of project {project}: {len(lines)} lines from {sources} independent sources"
    if not lines:
        return [f"problem {code} of project {project} has no evidence yet"]
    rows = []
    for line in lines:
        state = f"confirmed by {line['confirmed_by']}" if line["confirmed_by"] else "not confirmed"
        place = f" at {line['locator']}" if line["locator"] else ""
        who = f" ({line['speaker']})" if line["speaker"] else ""
        rows.append(
            f"- {line['code']} {line['signal']}, {line['label']}, {state}: {line['source_kind']} {line['source_uri']}"
            f"{place}{who}: {one_line(line['excerpt'], EXCERPT)}"
        )
    return [head, *rows]
