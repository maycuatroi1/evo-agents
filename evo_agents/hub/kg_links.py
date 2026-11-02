"""The hub side of ``evo-agents kg links propose --hub`` (``evo_agents.kg.links``): the project's review queue read from
the hub, and each proposal of the edge model sent through the route agents propose by, the MCP tool ``kg_propose``
(``evo_agents.hub.server.kg_decisions``). A proposal there changes nothing: a writer or the Curator accepts or rejects
it, and only an accepted one goes into the next build. Standard library only.

Each proposal names the project's decision revision it read (``base_rev``): the one GET .../decisions gives, then the
one each answer gives. A refusal (a node the hub's latest build lacks, a stale revision, a proposal already made) is
kept as that proposal's error and the next one is sent after reading the revision again.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.kg.serve import DATA_META

TOOL = "kg_propose"
REVIEW_RULES = ("dangling_mention", "ambiguous_match")


def _base(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}"


def _hub(hub: Hub | None) -> Hub:
    if hub is not None:
        return hub
    from evo_agents.hub.mcp_proxy import session_credentials

    credentials = session_credentials()
    return Hub(credentials.url, credentials.token)


def read_review(project: str, hub: Hub | None = None) -> list[dict]:
    """The open items of the project's review queue the edge model reads (REVIEW_RULES)."""
    hub = _hub(hub)
    items: list[dict] = []
    for rule in REVIEW_RULES:
        found = hub.call("GET", f"{_base(project)}/review?{urlencode({'state': 'open', 'rule': rule})}")
        items += found.get("items") or []
    return items


def _client(hub: Hub, project: str):
    from evo_agents.hub.mcp_proxy import McpClient, run_credentials

    run = run_credentials()
    if run is not None:
        return McpClient(run.url, run.token, project, AGENT_SINK, run=run.run)
    return McpClient(hub.url, hub.token, project, AGENT_SINK)


def _text(result: dict) -> str:
    return " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)).strip()


def send(project: str, proposals: list[dict], *, dry_run: bool = False, hub: Hub | None = None, client=None) -> list:
    """Send each proposal as a ``link`` proposal of the project; what came of each: the decision's number, or the
    hub's refusal under ``error``. ``dry_run`` reads the revision and sends nothing."""
    hub = _hub(hub)
    rev = int(hub.call("GET", f"{_base(project)}/decisions")["rev"])
    out = []
    if dry_run:
        return [{**_key(p), "number": None, "dry_run": True} for p in proposals]
    client = client or _client(hub, project)
    for p in proposals:
        args = {"kind": "link", "id": p["src"], "rel": p["rel"], "to": p["dst"], "reason": p["reason"][:1000]}
        try:
            result = client.call_tool(TOOL, {**args, "base_rev": rev, "project": project}, {DATA_META: True})
        except HubError as exc:
            out.append({**_key(p), "number": None, "error": str(exc)})
            continue
        data = result.get("structuredContent") or {}
        if result.get("isError") or "decision" not in data:
            out.append({**_key(p), "number": None, "error": _text(result) or "refused"})
            rev = int(hub.call("GET", f"{_base(project)}/decisions")["rev"])
            continue
        rev = int(data.get("rev", rev))
        out.append({**_key(p), "number": data["decision"]["number"], "state": data["decision"]["state"]})
    return out


def _key(p: dict) -> dict:
    return {"src": p["src"], "rel": p["rel"], "dst": p["dst"]}
