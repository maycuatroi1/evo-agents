"""Creative documents of the feature docs as typed nodes of the project's knowledge graph
(``evo_agents.hub.server.docs.typed``): a document declares its type, its links, its level and its audience in the YAML
block its Markdown opens with, and the next build holds it as a node of that kind with edges of those relations, under
the label it declares.

Checked first without a database: what a block declares against the project's ontology and ladder, the label it joins,
and the item the hub keeps of it. Then end to end on the hub: a campaign and the audience it targets, declared in the
brand space of a chat of alice and dave, are typed nodes with a ``targets`` edge, the label level and the audience of
that chat; carol, outside the chat, finds neither by kg_node or kg_search; an approval above alice's clearance shows to
her as locked, its kind and label and nothing it holds; and a document whose audience cannot be read stays out of the
graph. The documents are made up (Northwind, Lumen), as in ``tests.hub.test_docs_kg``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")

from sqlalchemy import update

from evo_agents.hub import tables
from evo_agents.hub.kg_sources import checked
from evo_agents.hub.server.docs import typed
from evo_agents.hub.server.docs.kg import document_item
from evo_agents.hub.server.docs.store import KgDocument
from tests.hub import pg

SOURCE = "plugin.docs"
LEVELS = ["public", "internal", "customer", "secret"]
ONTOLOGY = {
    "node_kinds": ["Campaign", "Audience", "Approval", "Insight"],
    "relations": [{"name": "targets", "domain": ["Campaign"], "range": ["Audience"]}, "approves"],
}
FRAME = typed.Frame.of(LEVELS, ONTOLOGY)
SAVED = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
CAMPAIGN, AUDIENCE, APPROVAL, LEAK = (
    "lumen-spring-campaign",
    "lumen-audience-makers",
    "lumen-spring-approval",
    "lumen-open-brief",
)
needs_db = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def front(**fields) -> str:
    lines = [f"{key}: {value}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---\n"


def campaign_body(chat: int | str) -> str:
    links = f"{{targets: {AUDIENCE}, follows: lumen-x, approves: Not_A_Slug}}"
    block = front(type="Campaign", links=links, level="internal", audience=f"chat:{chat}")
    return block + "# Spring\n\nThe Northwind spring campaign speaks to makers who build at night.\n"


# -- what a document declares --------------------------------------------------------------------------------------


def test_a_block_declares_the_type_links_level_and_audience_the_project_lets_it():
    found = typed.declared(campaign_body(12), FRAME)
    assert (found.kind, found.level, found.audience, found.closed) == ("Campaign", "internal", ("chat:12",), False)
    assert found.links == (("targets", AUDIENCE),)  # follows is no relation it knows, Not_A_Slug no slug
    assert found.text.startswith("# Spring") and "---" not in found.text
    plain = typed.declared("# Notes\n\nNo block.\n", FRAME)
    assert plain == typed.Declared("# Notes\n\nNo block.\n")
    hub_kind = typed.declared(front(type="Document", level="top"), FRAME)
    assert hub_kind.kind is None and hub_kind.level == "secret"  # a hub kind is refused, an unknown level is the top
    assert typed.declared(front(type="Campaign"), typed.Frame.of(LEVELS, None)).kind is None  # no ontology, no type
    broken = "---\ntype: [Campaign\n---\nText.\n"
    assert typed.declared(broken, FRAME) == typed.Declared(broken)  # a block that is no YAML declares nothing
    closed = typed.declared(front(type="Insight", audience="[chat:3, team:lumen]"), FRAME)
    assert closed.closed and closed.audience == ()


def test_the_label_is_the_stored_one_joined_with_the_declared_level_and_audience():
    found = typed.Declared("", level="customer", audience=("chat:4",))
    stored = {"level": "internal", "integrity": "U"}
    assert typed.label(stored, found, tuple(LEVELS)) == {"level": "customer", "integrity": "U", "audience": ["chat:4"]}
    lower = typed.Declared("", level="public")
    assert typed.label({"level": "secret"}, lower, tuple(LEVELS)) == {"level": "secret"}  # never lowered
    joined = typed.label({"level": "internal", "audience": ["chat:9"]}, found, tuple(LEVELS))
    assert joined["audience"] == ["chat:4", "chat:9"]


def test_the_item_of_a_typed_document_declares_its_node_its_edges_and_its_label():
    row = KgDocument(CAMPAIGN, "Spring campaign", {"level": "internal"}, 2, "alice", campaign_body(12), SAVED)
    targets = {AUDIENCE: typed.node_id(SOURCE, "Audience", AUDIENCE)}
    item = document_item(SOURCE, row, None, (typed.declared(row.body, FRAME), FRAME), targets)
    typed_id = f"{SOURCE}:campaign:{CAMPAIGN}"
    assert item["props"]["graph"] == {
        "nodes": [
            {
                "id": typed_id,
                "kind": "Campaign",
                "name": "Spring campaign",
                "props": {"document": CAMPAIGN, "locked": True},
            }
        ],
        "edges": [
            {"src": f"{SOURCE}:document:{CAMPAIGN}", "rel": "defines", "dst": typed_id},
            {"src": typed_id, "rel": "targets", "dst": f"{SOURCE}:audience:{AUDIENCE}"},
        ],
    }
    assert item["body"]["text"].startswith("# Spring")
    knowledge = {"policy": {"levels": LEVELS}}
    (kept,) = checked(SOURCE, "docs", {"items": [item]}, knowledge, "northwind").items
    assert kept["label"] == {"level": "internal", "integrity": "U", "audience": ["chat:12"]}


# -- on the hub ---------------------------------------------------------------------------------------------------


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    """Project alpha with the feature docs on (``tests.hub.test_docs_kg``), the ontology of a creative project, and a
    brand space: a chat of alice and dave with the project's agent, which carol is not in."""
    from tests.hub import live
    from tests.hub.test_kg import PROJECT, grant, open_hub

    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        grant(client, found.admin, "owner", "admin", "internal")
        found.owner = live.bearer(live.insert_token(hub_db, "owner"))
        on = client.post(f"/v1/projects/{PROJECT}/plugins/docs/enable", json={"permissions": []}, headers=found.owner)
        assert on.status_code == 200, on.text
        body = {"title": "Lumen brand space", "people": ["alice", "dave"], "agents": [PROJECT]}
        made = client.post("/v1/chats", json=body, headers=found.alice)
        assert made.status_code == 201, made.text
        found.space = made.json()["id"]
        yield found
    finally:
        client.__exit__(None, None, None)


def _rebuild(hub) -> None:
    from tests.hub.test_kg import PROJECT, build_rows, run_jobs, served

    assert hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice).status_code == 202
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    served(hub)
    built = build_rows(hub)[0]
    assert built["status"] == "succeeded", built.get("error")


@needs_db
def test_declared_documents_are_typed_nodes_of_their_brand_space_and_locked_above_the_clearance(hub, tmp_path):
    from tests.hub.live import sql
    from tests.hub.test_docs_kg import found_ids, save
    from tests.hub.test_kg import call_tool, tool_ok
    from tests.hub.test_kg_postgres_build import project_id
    from tests.hub.test_srs_tasks_plugin_kg import machine, node

    machine(hub, tmp_path)
    configs = tables.kg_configs
    sql(hub.db, update(configs).where(configs.c.project_id == project_id(hub)).values(ontology=ONTOLOGY))
    space = hub.space
    save(hub, CAMPAIGN, "Spring campaign", campaign_body(space))
    makers = front(type="Audience", audience=f"chat:{space}") + "Makers who build at night, in Lumen workshops.\n"
    save(hub, AUDIENCE, "Night makers", makers)
    approval = front(type="Approval", level="customer", links=f"{{approves: {CAMPAIGN}}}") + "Signed by the lead.\n"
    save(hub, APPROVAL, "Spring approval", approval, "owner")
    save(hub, LEAK, "Open brief", front(type="Insight", audience="team:lumen") + "A brief for everyone.\n")
    _rebuild(hub)

    typed_id, audience_id = f"{SOURCE}:campaign:{CAMPAIGN}", f"{SOURCE}:audience:{AUDIENCE}"
    for member in ("alice", "dave"):  # the members of the brand space
        campaign = node(hub, typed_id, member)
        assert (campaign["kind"], campaign["name"]) == ("Campaign", "Spring campaign")
        assert campaign["label"] == {
            "level": "internal",
            "location": "any",
            "integrity": "U",
            "audience": [f"chat:{space}"],
        }
    edges = tool_ok(hub, "kg_node", {"id": typed_id})["structuredContent"]
    assert {(e["rel"], e["node"]) for e in edges["out"]} >= {("targets", audience_id)}
    assert ("defines", f"{SOURCE}:document:{CAMPAIGN}") in {(e["rel"], e["node"]) for e in edges["in"]}
    assert node(hub, audience_id)["kind"] == "Audience"
    assert node(hub, f"{SOURCE}:document:{CAMPAIGN}")["label"]["audience"] == [f"chat:{space}"]
    assert typed_id in found_ids(hub, "spring campaign speaks to makers") or any(
        hit.startswith(f"{SOURCE}:document:{CAMPAIGN}") for hit in found_ids(hub, "spring campaign speaks to makers")
    )

    # carol is not in the brand space: neither node, nor the document, nor a hit of its text
    for hidden in (typed_id, audience_id, f"{SOURCE}:document:{CAMPAIGN}"):
        assert call_tool(hub, "kg_node", {"id": hidden}, member="carol").json()["isError"], hidden
    hits = found_ids(hub, "spring campaign speaks to makers", "carol") + found_ids(hub, "night makers", "carol")
    assert not any(hit.startswith((typed_id, audience_id, f"{SOURCE}:document:")) for hit in hits), hits

    # the approval is labelled customer, above alice's grant: she learns it exists, its kind and label, nothing more
    approval_id = f"{SOURCE}:approval:{APPROVAL}"
    locked = tool_ok(hub, "kg_node", {"id": approval_id})["structuredContent"]
    assert locked["node"] == {
        "id": approval_id,
        "kind": "Approval",
        "label": {"level": "customer", "location": "any", "integrity": "U"},
        "locked": True,
    }
    assert (locked["out"], locked["in"], locked["evidence"]) == ([], [], [])
    assert "Signed by the lead" not in str(locked) and "Spring approval" not in str(locked)
    assert node(hub, approval_id, "dave")["name"] == "Spring approval"  # dave's grant reaches it
    document = call_tool(hub, "kg_node", {"id": f"{SOURCE}:document:{APPROVAL}"}).json()
    assert document["isError"]  # the untyped document node does not ask to show locked

    # a document whose audience cannot be read stays out of the graph, for every member
    for gone in (f"{SOURCE}:insight:{LEAK}", f"{SOURCE}:document:{LEAK}"):
        assert call_tool(hub, "kg_node", {"id": gone}, member="dave").json()["isError"], gone
