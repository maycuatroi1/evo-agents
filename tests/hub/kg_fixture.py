"""A small synthetic knowledge graph for the hub's web reads, shared by ``tests.hub.test_kg_api`` and the web's
Playwright stack (``web/e2e/kg_seed.py``).

One harness with two sources of the fake connector (``tests.kg.fakes``):

- ``docs``, labelled internal: a runbook and an architecture page that mention requirements KB-01 to KB-03, and
  NOTES short notes that all mention KB-77, so requirement KB-77 has more than 150 neighbours.
- ``deals``, labelled customer: one contract that mentions KB-01. A member whose grant stops at internal must never
  receive anything of it.

The project is registered with a hub sink that clears customer, so both sources may be pushed, an agent sink that
clears customer (``AGENT``) and one that clears internal only (``NARROW``). The text is Vietnamese, as the team's
documents are. Nothing here touches ~/.evo/kg: the harness and its kg home live under the directory given.
"""

from __future__ import annotations

import json
from pathlib import Path

LEVELS = ["public", "internal", "customer", "secret"]
AGENT = "claude-code@anthropic"
NARROW = "narrow-agent"
NOTES = 160
HUB_NODE = "requirement:KB-77"  # more than 150 neighbours
SHARED_NODE = "requirement:KB-01"  # neighbours in docs and in deals
RUNBOOK = "docs:doc:runbook"
RUNBOOK_SECTION = "docs:doc:runbook#sổ-tay-vận-hành"  # mentions KB-01
CONTRACT = "deals:doc:acme-contract"
CONTRACT_SECTION = "deals:doc:acme-contract#hợp-đồng-acme"

KNOWLEDGE = """\
version: 1
project: {project}
policy:
  levels: [public, internal, customer, secret]
  sinks:
    - {{id: {agent}, kind: agent-session, clearance: {{level: customer}}}}
    - {{id: {narrow}, kind: agent-session, clearance: {{level: internal}}}}
    - {{id: hub, kind: hub, clearance: {{level: customer}}}}
identifiers:
  - {{kind: Requirement, pattern: "KB-[0-9]+"}}
sources:
  - id: docs
    connector: "python:tests.kg.fakes:run"
    config: {{file: "{docs}"}}
    label: {{level: internal, integrity: U}}
  - id: deals
    connector: "python:tests.kg.fakes:run"
    config: {{file: "{deals}"}}
    label: {{level: customer, integrity: U}}
"""

DOCS = [
    {
        "key": "runbook",
        "rev": "1",
        "text": (
            "# Sổ tay vận hành\n\nCách chạy hub hằng ngày. Xem KB-01 và KB-02.\n\n"
            "## Cài đặt\n\nCài api, worker và web theo KB-01.\n\n"
            "## Sao lưu\n\nSao lưu Postgres mỗi đêm, xem KB-03.\n"
        ),
    },
    {
        "key": "architecture",
        "rev": "1",
        "text": (
            "# Kiến trúc hub\n\nHub gồm api, worker và web. KB-02.\n\n"
            "## Đồ thị tri thức\n\nWorker dựng đồ thị từ các lượt đẩy. KB-03.\n"
        ),
    },
    *(
        {"key": f"note-{i:03d}", "rev": "1", "text": f"# Ghi chú {i}\n\nGhi chú số {i} về yêu cầu KB-77.\n"}
        for i in range(1, NOTES + 1)
    ),
]
DEALS = [
    {
        "key": "acme-contract",
        "rev": "1",
        "text": "# Hợp đồng Acme\n\nĐiều khoản giá riêng cho khách hàng Acme. KB-01.\n",
    }
]


def registration(project: str) -> dict:
    """The body of PUT /v1/projects/{project} for the fixture's harness."""
    return {
        "levels": LEVELS,
        "locations": ["any"],
        "default_label": {"level": "internal"},
        "sinks": [
            {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
            {"id": NARROW, "kind": "agent-session", "clearance": {"level": "internal"}},
            {"id": "hub", "kind": "hub", "clearance": {"level": "customer"}},
        ],
        "repos": [],
        "harness": {"name": project, "workspace": "~/ws", "path": f"{project}-harness"},
    }


class Harness:
    """The fixture's harness of ``project`` under ``base``, with a kg home of its own."""

    def __init__(self, base: Path, project: str):
        self.name = project
        self.root = base / project
        self.harness = self.root / f"{project}-harness"
        self.home = self.root / "kg-home"
        self.harness.mkdir(parents=True, exist_ok=True)
        self.files = {"docs": self.root / "docs.json", "deals": self.root / "deals.json"}
        (self.harness / "harness.yaml").write_text(
            f"name: {project}\nrepos: []\nknowledge_file: knowledge.yaml\n", encoding="utf-8"
        )
        (self.harness / "knowledge.yaml").write_text(
            KNOWLEDGE.format(
                project=project, agent=AGENT, narrow=NARROW, docs=self.files["docs"], deals=self.files["deals"]
            ),
            encoding="utf-8",
        )

    def project(self):
        from evo_agents.kg.project import load_project_at

        return load_project_at(self.harness, self.home)

    def sync(self, extra: str | None = None) -> list[str]:
        """One run of each source; their run ids. ``extra`` adds a short note of that key to docs, which changes the
        graph's content."""
        from evo_agents.kg.sync import sync_project

        note = {"key": extra, "rev": "1", "text": f"# {extra}\n\nThêm một ghi chú, KB-01.\n"}
        docs = DOCS + ([note] if extra else [])
        self.files["docs"].write_text(json.dumps({"items": docs}), encoding="utf-8")
        self.files["deals"].write_text(json.dumps({"items": DEALS}), encoding="utf-8")
        results = sync_project(self.project(), ["docs", "deals"])
        problems = [r.issues for r in results if not r.ok]
        if problems:
            raise RuntimeError(f"the fixture's sync failed: {problems}")
        return [r.run_id for r in results]

    def push(self, hub):
        """Push the runs through ``hub`` (anything with ``url`` and ``call(method, path, body)``); the report."""
        from evo_agents.hub.kg_push import Pusher

        report = Pusher(hub, self.project()).push()
        if not report.ok:
            raise RuntimeError(f"the fixture's push failed: {report.errors}")
        return report
