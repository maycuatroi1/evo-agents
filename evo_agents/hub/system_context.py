"""What an agent of an agent system knows of it, the brief of an ask, and the numbers of a trace (``docs/hub.md``,
Agent systems; ``evo_agents.hub.system_spec``). Pure functions over a checked spec and JSON values, standard library
only but the spec module, so the hub, the worker's commands and the tests share them.

- ``system_lines``: the System section of the prompt of a run of an agent in a system: the system's purpose, the
  agents it hands work to and those that hand it work (the interacts edges out and in, with @handle and description),
  the knowledge graphs it uses (its uses_kg edges, which the kg tools read when no ``graph`` is named), the
  Orchestrator it escalates to (with ESCALATE_COMMAND), and the rules of asking: a brief (goal, output, done-when), a reason for an agent off
  its edges, forwarding an answer verbatim, the budget of a trace. It has a byte budget of its own, SYSTEM_BYTES.
- ``brief_lines``: the brief an ask gave (``evo-agents worker agent ask --goal ... --done-when``), at the head of the
  prompt of the run it made, within BRIEF_BYTES.
- ``off_edge_refusal``: why an ask from an agent of a system to an agent off its interacts edges, with no reason, is
  refused, naming the agents it may ask and how to give a reason.
- ``budget_problem``: why a trace takes no new ask or mention, past ``limits.trace_usd`` or ``limits.trace_runs``.
- ``metrics``: the numbers of each node and edge of a system over the spans of its traces: count, errors, off-design,
  p50 and last duration, tokens and cost.

A trace is named and shaped as OpenTelemetry GenAI names agent spans: ``invoke_agent`` for a run of an agent,
``execute_tool`` for what it does through the hub (an ask, a mention, a forward, a query of a knowledge graph), with
the attributes of ATTRIBUTES. Content is not copied into a span: a span names the message or the run that holds it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from evo_agents.hub import system_spec

SYSTEM_BYTES = 4 * 1024  # the System section of a prompt
BRIEF_BYTES = 3 * 1024  # the brief at the head of the prompt of a run an ask made
BRIEF_FIELDS = ("goal", "output", "bounds", "done_when")  # the text fields of a brief, in the order the prompt shows
BRIEF_LABELS = {"goal": "Goal", "output": "Output", "bounds": "Bounds", "done_when": "Done when"}
BRIEF_FIELD_CHARS = 2000  # one text field of a brief
MAX_REFS = 10  # the refs of a brief (--ref, repeatable)
REF_CHARS = 300
REASON_CHARS = 500  # the reason of an ask off the agent's interacts edges

SPAN_NAMES = ("invoke_agent", "execute_tool")  # tables_traces.SPAN_NAMES
SPAN_STATUSES = ("running", "ok", "error", "refused")  # tables_traces.SPAN_STATUSES
INVOKE, TOOL = SPAN_NAMES
TOOL_ASK = "agent_ask"
TOOL_MENTION = "chat_mention"
TOOL_FORWARD = "agent_forward"
TOOL_ESCALATE = "agent_escalate"  # an escalation to the Orchestrator (``orchestrator.escalate``)
TOOL_REASSIGN = "orchestrator_reassign"  # the Orchestrator gives the work of a thread to another agent
TOOLS = (TOOL_ASK, TOOL_MENTION, TOOL_FORWARD)  # and kg_search, kg_context ... for a query of a knowledge graph
ATTRIBUTES = (  # the OpenTelemetry GenAI attributes a span carries
    "gen_ai.operation.name",
    "gen_ai.agent.name",
    "gen_ai.conversation.id",
    "gen_ai.data_source.id",
    "gen_ai.tool.name",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
)
ENTRY = "entry"  # the kind of node a person's message comes from
ASK_COMMAND = "evo-agents worker agent ask"
FORWARD_COMMAND = "evo-agents worker agent forward"
ESCALATE_COMMAND = "evo-agents worker escalate"  # docs/workers.md, Escalation
CUT = "\n[... the rest of the system is left out]"


def _clip(text: str, limit: int, mark: str = CUT) -> str:
    data = text.encode()
    if len(data) <= limit:
        return text
    keep = limit - len(mark.encode())
    return data[:keep].decode(errors="ignore").rstrip() + mark


def _nodes(spec: dict) -> dict[str, dict]:
    return {n["id"]: n for n in spec.get("nodes") or ()}


def node_of(spec: dict, handle: str) -> str | None:
    """The id of the node of agent ``handle`` in a checked spec (an agent or the orchestrator); None for none."""
    for node in spec.get("nodes") or ():
        if node["kind"] in system_spec.PINNED_KINDS and node.get("profile") == handle:
            return node["id"]
    return None


def entry_node(spec: dict) -> str | None:
    return next((n["id"] for n in spec.get("nodes") or () if n["kind"] == ENTRY), None)


def external_node(handle: str) -> str:
    """The node a trace shows for agent ``handle``, which the spec lacks."""
    return f"@{handle}"


def _edges(spec: dict, kind: str, *, start: str | None = None, end: str | None = None) -> list[dict]:
    return [
        e
        for e in spec.get("edges") or ()
        if e["kind"] == kind and (start is None or e["from"] == start) and (end is None or e["to"] == end)
    ]


def edge_targets(spec: dict, handle: str) -> list[str]:
    """The handles of the agents agent ``handle`` hands work to (its interacts edges out), in the spec's order."""
    nodes, node = _nodes(spec), node_of(spec, handle)
    if node is None:
        return []
    found = [nodes[e["to"]].get("profile") for e in _edges(spec, "interacts", start=node) if e["to"] in nodes]
    return [target for target in dict.fromkeys(found) if target]


def on_design(spec: dict, from_node: str | None, to_node: str | None) -> bool:
    """Whether the spec has an interacts edge from node ``from_node`` to node ``to_node``, or an edge of the
    Orchestrator between them: an escalates edge from the agent, or its orchestrates edge in either direction (the
    Orchestrator gives work to an agent it orchestrates, and is called for that agent)."""
    if from_node is None or to_node is None:
        return False
    return bool(
        _edges(spec, "interacts", start=from_node, end=to_node)
        or _edges(spec, "escalates", start=from_node, end=to_node)
        or _edges(spec, "orchestrates", start=from_node, end=to_node)
        or _edges(spec, "orchestrates", start=to_node, end=from_node)
    )


def kg_graphs(spec: dict, handle: str) -> list[str]:
    """The named graphs agent ``handle`` uses (its uses_kg edges), ``default`` for every source, in the spec's order."""
    nodes, node = _nodes(spec), node_of(spec, handle)
    if node is None:
        return []
    found = [nodes[e["to"]].get("graph") for e in _edges(spec, "uses_kg", start=node) if e["to"] in nodes]
    return [graph for graph in dict.fromkeys(found) if graph]


def orchestrator_of(spec: dict, handle: str) -> tuple[str, str] | None:
    """(the handle of the Orchestrator agent ``handle`` escalates to, the edge's description); None for none."""
    nodes, node = _nodes(spec), node_of(spec, handle)
    for edge in _edges(spec, "escalates", start=node) if node else ():
        target = nodes.get(edge["to"], {}).get("profile")
        if target:
            return target, edge.get("description") or ""
    return None


def lead_of(spec: dict, handle: str) -> str | None:
    """The handle of the Orchestrator that orchestrates agent ``handle`` (an orchestrates edge to its node); None for
    an agent no Orchestrator orchestrates, which keeps the triage of its project."""
    nodes, node = _nodes(spec), node_of(spec, handle)
    for edge in _edges(spec, "orchestrates", end=node) if node else ():
        lead = nodes.get(edge["from"], {})
        if lead.get("kind") == "orchestrator" and lead.get("profile"):
            return lead["profile"]
    return None


def escalation_target(spec: dict, handle: str) -> str | None:
    """The Orchestrator agent ``handle`` escalates to: the one that orchestrates it, else the one of its escalates
    edge; None for none."""
    lead = lead_of(spec, handle)
    if lead is not None:
        return lead
    found = orchestrator_of(spec, handle)
    return None if found is None else found[0]


def node_kind(spec: dict, handle: str) -> str | None:
    """The kind of the node of agent ``handle`` (agent or orchestrator); None when the spec lacks it."""
    node = node_of(spec, handle)
    return None if node is None else _nodes(spec)[node]["kind"]


def limits_of(spec: dict) -> tuple[float, int]:
    """(trace_usd, trace_runs) of a spec, the defaults of ``system_spec.LIMITS`` for what it does not set."""
    limits = spec.get("limits") or {}
    usd = limits.get("trace_usd", system_spec.LIMITS["trace_usd"][0])
    count = limits.get("trace_runs", system_spec.LIMITS["trace_runs"][0])
    return float(usd), int(count)


@dataclass(frozen=True)
class Place:
    """Where an agent stands in a system: the system's ``slug`` and checked ``spec``, the agent's ``handle`` and its
    ``node``."""

    slug: str
    spec: dict
    handle: str
    node: str


def place_of(slug: str, spec: dict, handle: str) -> Place | None:
    node = node_of(spec, handle)
    return None if node is None else Place(slug, spec, handle, node)


def _who(nodes: dict, node_id: str) -> str:
    node = nodes.get(node_id) or {}
    if node.get("kind") == ENTRY:
        return "the people who write to the system"
    if node.get("profile"):
        return f"@{node['profile']}"
    return node.get("label") or node_id


def system_lines(place: Place) -> list[str]:
    """The System section of the prompt of a run of agent ``place.handle``, at most SYSTEM_BYTES."""
    spec, nodes = place.spec, _nodes(place.spec)
    usd, count = limits_of(spec)
    lines = [
        f"# Your agent system: {spec.get('name') or place.slug} ({place.slug})",
        "",
        f"You are node {place.node} of agent system {place.slug} of this project.",
    ]
    if spec.get("purpose"):
        lines.append(f"Its purpose: {spec['purpose']}")
    out = _edges(spec, "interacts", start=place.node)
    if out:
        lines += ["", "Agents you hand work to (with `" + ASK_COMMAND + "`):"]
        lines += [f"- {_who(nodes, e['to'])}: {e.get('description') or '(no description)'}" for e in out]
    came = _edges(spec, "interacts", end=place.node)
    if came:
        lines += ["", "Who hands you work:"]
        lines += [f"- {_who(nodes, e['from'])}: {e.get('description') or '(no description)'}" for e in came]
    graphs = [(nodes[e["to"]].get("graph"), e.get("description")) for e in _edges(spec, "uses_kg", start=place.node)]
    graphs = [(graph, about) for graph, about in graphs if graph]
    if graphs:
        lines += [
            "",
            "Knowledge graphs you use (kg_search, kg_context and the other kg tools read them when you name no graph):",
        ]
        lines += [f"- {graph}: {about or '(no description)'}" for graph, about in graphs]
    lead = orchestrator_of(spec, place.handle)
    if lead is not None:
        lines += [
            "",
            f"The Orchestrator you escalate to (with `{ESCALATE_COMMAND} --question TEXT` and the brief flags of an ask; "
            f"an escalation adds no hop, an ask of it would): @{lead[0]}: {lead[1] or '(no description)'}",
        ]
    lines += [
        "",
        "How you work in it:",
        "- Ask only the agents you hand work to. An ask of another agent is refused unless you give `--reason WHY`; "
        "the hub then runs it and records it as off design, with your reason.",
        "- Give each ask a brief: `--goal` (what to reach), `--output` (what to send back) and `--done-when` (when "
        "it is finished); add `--bounds` (what not to do) and `--ref` (repeatable) when they help.",
        f"- To pass an answer of a run you asked on to this thread as it is, use `{FORWARD_COMMAND} --run RUN` "
        "rather than retyping it.",
        f"- A trace of this system runs at most {count} runs and {usd:g} USD; past either, the hub refuses new asks "
        "and mentions in it.",
    ]
    return _clip("\n".join(lines), SYSTEM_BYTES).split("\n")


def clean_brief(brief: dict | None) -> dict:
    """The fields of ``brief`` that hold something, stripped and bounded; {} for no brief."""
    if not brief:
        return {}
    found = {}
    for field in (*BRIEF_FIELDS, "reason"):
        value = brief.get(field)
        if isinstance(value, str) and value.strip():
            found[field] = value.strip()[: REASON_CHARS if field == "reason" else BRIEF_FIELD_CHARS]
    refs = [ref.strip()[:REF_CHARS] for ref in brief.get("refs") or () if isinstance(ref, str) and ref.strip()]
    if refs:
        found["refs"] = refs[:MAX_REFS]
    return found


def brief_present(brief: dict | None) -> list[str]:
    """The fields of a brief that were given, for its span: of BRIEF_FIELDS and refs."""
    found = clean_brief(brief)
    return [field for field in (*BRIEF_FIELDS, "refs") if field in found]


def brief_lines(brief: dict | None, asker: str | None = None, run_id: int | None = None) -> list[str]:
    """The brief block at the head of the prompt of a run an ask made, at most BRIEF_BYTES; [] for no brief."""
    found = clean_brief(brief)
    if not any(field in found for field in (*BRIEF_FIELDS, "refs")):
        return []
    who = f" from @{asker}" + (f" (run #{run_id})" if run_id else "") if asker else ""
    lines = [f"# Your brief{who}"]
    lines += [f"{BRIEF_LABELS[field]}: {found[field]}" for field in BRIEF_FIELDS if field in found]
    if found.get("refs"):
        lines.append("Refs: " + "; ".join(found["refs"]))
    lines.append("Your last message of the turn answers it: meet the output and the done-when it names.")
    return _clip("\n".join(lines), BRIEF_BYTES, "\n[... the rest of the brief is left out]").split("\n")


def brief_text(brief: dict | None) -> str:
    """The brief as the question message shows it under the question; '' for none."""
    found = clean_brief(brief)
    lines = [f"{BRIEF_LABELS[field]}: {found[field]}" for field in BRIEF_FIELDS if field in found]
    if found.get("refs"):
        lines.append("Refs: " + "; ".join(found["refs"]))
    return "\n".join(lines)


def off_edge_refusal(handle: str, target: str, targets: list[str]) -> str:
    """Why agent ``handle`` may not ask ``target``, off its interacts edges ``targets``, without a reason."""
    named = ", ".join(f"@{name}" for name in targets) or "no agent"
    return (
        f"@{target} is off the interacts edges of @{handle} in its agent system, which lead to {named}. Ask one of "
        f"them, or ask @{target} again with `--reason WHY`: the hub then runs it and records it as off design."
    )


BUDGET_REFUSAL = "the trace reached its budget"  # how each refusal of ``budget_problem`` starts


def budget_problem(spec: dict, runs: int, usd: float) -> str | None:
    """Why a trace of ``runs`` runs that cost ``usd`` takes no new ask or mention under the limits of ``spec``; None
    while it does."""
    limit_usd, limit_runs = limits_of(spec)
    if runs >= limit_runs:
        return (
            f"the trace reached its budget of {limit_runs} runs (limits.trace_runs of the system): no new ask or "
            "mention in it; answer with what you have"
        )
    if usd >= limit_usd:
        return (
            f"the trace reached its budget of {limit_usd:g} USD (limits.trace_usd of the system; {usd:.2f} spent): no "
            "new ask or mention in it; answer with what you have"
        )
    return None


def _p50(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def metrics(spans: list[dict]) -> dict:
    """The numbers of each node and edge over ``spans``, the invoke_agent spans of a system's traces, each a dict of
    node, from_node, status, off_design, resume (a turn that goes on in a session, counted on its node alone), seconds
    (None while running), started_at, input_tokens, output_tokens and usd: ``{"nodes": [...], "edges": [...]}``,
    each item with count, errors, off_design, p50_seconds, last_seconds, last_at, input_tokens, output_tokens and
    usd."""
    nodes: dict[str, list[dict]] = {}
    edges: dict[tuple[str, str], list[dict]] = {}
    for span in spans:
        nodes.setdefault(span["node"], []).append(span)
        if span.get("from_node") and not span.get("resume"):  # a turn that goes on in a session crosses no edge
            edges.setdefault((span["from_node"], span["node"]), []).append(span)

    def numbers(found: list[dict]) -> dict:
        found = sorted(found, key=lambda s: str(s.get("started_at") or ""))
        seconds = [s["seconds"] for s in found if s.get("seconds") is not None]
        return {
            "count": len(found),
            "errors": sum(1 for s in found if s.get("status") == "error"),
            "off_design": sum(1 for s in found if s.get("off_design")),
            "p50_seconds": _p50(seconds),
            "last_seconds": seconds[-1] if seconds else None,
            "last_at": found[-1].get("started_at") if found else None,
            "input_tokens": sum(int(s.get("input_tokens") or 0) for s in found),
            "output_tokens": sum(int(s.get("output_tokens") or 0) for s in found),
            "usd": round(sum(float(s.get("usd") or 0) for s in found), 6),
        }

    return {
        "nodes": [{"node": node, **numbers(found)} for node, found in sorted(nodes.items())],
        "edges": [
            {"from_node": start, "to_node": end, **numbers(found)} for (start, end), found in sorted(edges.items())
        ],
    }
