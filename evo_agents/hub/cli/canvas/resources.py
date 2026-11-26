"""The routes of the core plugin canvas ``evo-agents hub canvas`` asks, under /v1/x/canvas/{project}/canvases
(``evo_agents.hub.server.canvas``, which this client never imports): :class:`Canvases`, the one gateway.

A refusal lists on stderr, one line each, every claim of a conflict (the rev it is at, who changed it, what differs)
and every problem the hub found. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from typing import Any

from evo_agents.hub.cli.features import PluginRoutes


def conflict_line(item: dict) -> str:
    changes = "; ".join(f"{c['field']}: {c['before']!r} -> {c['after']!r}" for c in item.get("changes") or [])
    head = f"  {item['claim']}: at rev {item['rev']}, changed by {item['changed_by']}; you read rev {item['base_rev']}"
    return head + (f": {changes}" if changes else "")


def archived(given: bool) -> dict:
    return {"archived": "true" if given else None}


class Canvases(PluginRoutes):
    """The canvases of a project, their claims, evidence, proposals and baselines (Gateway)."""

    feature = "canvas"

    def problems(self, detail: Any) -> list[str]:
        lines = []
        for item in detail:
            if isinstance(item, dict) and "changed_by" in item:
                lines.append(conflict_line(item))
            elif isinstance(item, dict) and "level" in item:
                claim = f" {item['claim']}" if item.get("claim") else ""
                lines.append(f"  {item['level']}{claim}: {item['message']}")
        return lines

    # Canvases and claims

    def list(self, with_archived: bool) -> list[dict]:
        return self.call("GET", "canvases", query=archived(with_archived))

    def get(self, slug: str, with_archived: bool) -> dict:
        return self.call("GET", "canvases", slug, query=archived(with_archived))

    def claim(self, slug: str, code: str) -> dict:
        return self.call("GET", "canvases", slug, "claims", code)

    def create(self, body: dict) -> dict:
        return self.call("POST", "canvases", body=body)

    def edit(self, slug: str, body: dict) -> dict:
        return self.call("POST", "canvases", slug, "edit", body=body)

    def undo(self, slug: str, body: dict) -> dict:
        return self.call("POST", "canvases", slug, "undo", body=body)

    # Evidence

    def add_evidence(self, slug: str, claim: str, body: dict) -> dict:
        return self.call("POST", "canvases", slug, "claims", claim, "evidence", body=body)

    def withdraw_evidence(self, slug: str, claim: str, evidence: int, reason: str) -> dict:
        return self.call(
            "POST", "canvases", slug, "claims", claim, "evidence", evidence, "withdraw", body={"reason": reason}
        )

    def confirm_evidence(self, slug: str, claim: str, evidence: int) -> dict:
        return self.call("POST", "canvases", slug, "claims", claim, "evidence", evidence, "confirm")

    # Proposals and brainstorms

    def proposals(self, slug: str, statuses: list[str]) -> list[dict]:
        return self.call("GET", "canvases", slug, "proposals", query=[("status", status) for status in statuses])

    def accept(self, slug: str, proposal: int) -> dict:
        return self.call("POST", "canvases", slug, "proposals", proposal, "accept")

    def reject(self, slug: str, proposal: int, reason: str) -> dict:
        return self.call("POST", "canvases", slug, "proposals", proposal, "reject", body={"reason": reason})

    def brainstorm(self, slug: str, body: dict) -> dict:
        return self.call("POST", "canvases", slug, "brainstorm", body=body)

    # Baselines

    def baselines(self, slug: str) -> list[dict]:
        return self.call("GET", "canvases", slug, "baselines")

    def baseline(self, slug: str, number: int) -> dict:
        return self.call("GET", "canvases", slug, "baselines", number)

    def open_baseline(self, slug: str, claim: str) -> dict:
        return self.call("POST", "canvases", slug, "baselines", body={"claim": claim})

    def decide(self, slug: str, number: int, claim: str, body: dict) -> dict:
        return self.call("POST", "canvases", slug, "baselines", number, "items", claim, "decide", body=body)

    # evo-canvas/1

    def export(self, slug: str) -> dict:
        return self.call("GET", "canvases", slug, "export")

    def import_(self, body: dict) -> dict:
        return self.call("POST", "canvases", "import", body=body)
