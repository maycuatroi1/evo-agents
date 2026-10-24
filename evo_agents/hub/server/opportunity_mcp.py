"""What the MCP tools ``opportunity_list``, ``opportunity_show`` and ``opportunity_evidence`` answer (plan
opportunity-intake): the text a model reads and the JSON beside it. ``evo_agents.hub.server.mcp`` resolves the caller's
access and calls ``answer``; the data is the routes' own (``opportunities``, ``opportunity_evidence``), so a reader
without a grant on another project sees a hidden count and no code of it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import opportunities as model
from evo_agents.hub import problems
from evo_agents.hub.server import opportunities, opportunity_evidence
from evo_agents.hub.server.mcp_text import EXCERPT, one_line
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal


def line(item: dict) -> str:
    """An opportunity of ``opportunity_list`` in one line: its code, status, rank, owner and statement."""
    return (
        f"{item['code']} ({model.status_text(item)}, rank {item['rank']}, owner {item['owner']}, "
        f"{model.window_text(item)}): {one_line(item['statement'], EXCERPT)}"
    )


def lines(project: str, item: dict) -> list[str]:
    """What ``opportunity_show`` says of an opportunity (its JSON, as ProjectOpportunityShown has it)."""
    answers = ", ".join(item["answers"]) or "none"
    hidden = f" (and {item['hidden_answers']} of projects you hold no grant on)" if item["hidden_answers"] else ""
    return [
        f"# Opportunity {item['code']} of project {project}, revision {item['revision']}",
        f"statement: {item['statement']}",
        f"beneficiary: {item['beneficiary']}",
        f"leverage: {item['leverage']}",
        f"window: {model.window_text(item)}",
        f"answers: {answers}{hidden}",
        f"metric: {problems.metric_text(item['metric'])}",
        f"rank: {item['rank']}; owner: {item['owner']}; status: {model.status_text(item)}",
        f"evidence: {item['evidence_count']} lines from {item['source_count']} independent sources",
    ]


def evidence_text(code: str, project: str, found: list[dict]) -> list[str]:
    """What ``opportunity_evidence`` says of an opportunity's lines (as OpportunityEvidence has them)."""
    if not found:
        return [f"opportunity {code} of project {project} has no evidence yet"]
    sources = problems.sources_of(found)
    rows = [f"# Evidence of opportunity {code} of project {project}: {len(found)} lines from {sources} sources"]
    for item in found:
        state = f"confirmed by {item['confirmed_by']}" if item["confirmed_by"] else "not confirmed"
        place = f" at {item['locator']}" if item["locator"] else ""
        who = f" ({item['speaker']})" if item["speaker"] else ""
        rows.append(
            f"- {item['code']} {item['signal']}, {item['label']}, {state}: {item['source_kind']} "
            f"{item['source_uri']}{place}{who}: {one_line(item['excerpt'], EXCERPT)}"
        )
    return rows


async def answer(
    engine: AsyncEngine, user: Principal, access: ProjectAccess, name: str, arguments: dict
) -> tuple[str, dict]:
    """The text and the data of tool ``name`` for ``user``, who reads project ``access``."""
    project = access.name
    async with engine.begin() as conn:
        if name == "opportunity_list":
            status = arguments.get("status")
            found = await opportunities.listed(conn, user, access, status)
            items = [item.model_dump(mode="json") for item in found]
            none = f"project {project} has no opportunity{' ' + status if status else ''} on the hub"
            text = "\n".join(line(item) for item in items) or none
            return text, {"project": project, "opportunities": items, "summary": f"{len(items)} opportunities"}
        if name == "opportunity_show":
            shown = await opportunities.shown(conn, user, access, arguments["code"], arguments.get("revision"))
            data = shown.model_dump(mode="json")
            return "\n".join(lines(project, data)), {
                "project": project,
                "opportunity": data,
                "summary": f"opportunity {shown.code}",
            }
        listed = await opportunity_evidence.listed(conn, access, arguments["code"])
    found = [item.model_dump(mode="json") for item in listed]
    summary = f"{len(found)} lines of evidence from {problems.sources_of(found)} sources"
    data = {"project": project, "opportunity": arguments["code"], "evidence": found, "summary": summary}
    return "\n".join(evidence_text(arguments["code"], project, found)), data
