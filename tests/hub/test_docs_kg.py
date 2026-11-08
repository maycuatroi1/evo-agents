"""The knowledge graph of a project on the hub that has the feature docs on: the worker's build reads the feature's
source (``evo_agents.hub.server.docs.kg``) beside the runs machines push.

Checked here, end to end on the hub: saving a document queues a build, which joins the one that waits, and a save that
changes nothing queues none, and a build of unchanged documents reuses the artifact of the build before; the build holds
each document at its latest version as a Document node with its slug, version and author, and kg_search finds it by a
phrase of its body; each document carries its own label, so a member whose grant is below it sees none of it while a
document labelled public shows to that member; a new version replaces the text the graph holds; and a project that turns
the feature off has its documents leave the graph. The documents are made up (Northwind, Lumen); the api and the worker
run in this process and the blob store is moto's, as in ``tests.hub.test_kg``.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub import live
from tests.hub.test_kg import PROJECT, build_rows, call_tool, grant, open_hub, run_jobs, tool_ok
from tests.hub.test_srs_tasks_plugin_kg import machine, node, queued

BASE = f"/v1/x/docs/{PROJECT}/documents"
RESEARCH = "research-northwind-budgets"
GUIDE = "lumen-guide"
RESEARCH_ID = f"plugin.docs:document:{RESEARCH}"
GUIDE_ID = f"plugin.docs:document:{GUIDE}"
BODY = (
    "Notes on agent budgets.\n\n"
    "## Loop guards\n\n"
    "A Northwind agent stops after three hops without progress and asks the lead.\n\n"
    "## Costs\n\n"
    "Each run reports its tokens.\n"
)


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    """Project alpha of ``tests.hub.test_kg`` with an admin, owner, who turned the feature docs on."""
    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        grant(client, found.admin, "owner", "admin", "internal")
        found.owner = live.bearer(live.insert_token(hub_db, "owner"))
        enabled = client.post(
            f"/v1/projects/{PROJECT}/plugins/docs/enable", json={"permissions": []}, headers=found.owner
        )
        assert enabled.status_code == 200, enabled.text
        yield found
    finally:
        client.__exit__(None, None, None)


def save(hub, slug: str, title: str, body: str, member: str = "alice", **extra) -> dict:
    answer = hub.client.put(
        f"{BASE}/{slug}", json={"title": title, "body": body, **extra}, headers=getattr(hub, member)
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


def found_ids(hub, text: str, member: str = "alice") -> list[str]:
    results = tool_ok(hub, "kg_search", {"query": text}, member=member)["structuredContent"]["results"]
    return [hit["id"] for hit in results]


def test_docs_graph_on_the_hub_holds_each_document_at_its_latest_version_under_its_label(hub, tmp_path):
    machine(hub, tmp_path)  # the knowledge config, and a first build
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert build_rows(hub)[0]["status"] == "succeeded"
    assert queued(hub) == 0

    save(hub, RESEARCH, "Budgets and loops", BODY)
    assert queued(hub) == 1  # the save queued a build
    guide = "# Lumen\n\nThe lumen board lists every open ticket.\n"
    save(hub, GUIDE, "Lumen guide", guide, "owner", label={"level": "public"})
    assert queued(hub) == 1  # the second save's build joined the one that waits
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    built = build_rows(hub)[0]
    assert built["status"] == "succeeded", built.get("error")

    research = node(hub, RESEARCH_ID)
    assert (research["kind"], research["name"]) == ("Document", "Budgets and loops")
    assert research["label"]["level"] == "internal"  # the project's label, which the document took
    assert {key: research["props"][key] for key in ("document", "version", "author")} == {
        "document": RESEARCH,
        "version": 1,
        "author": "alice",
    }
    assert node(hub, f"{RESEARCH_ID}#loop-guards")["kind"] == "Section"
    hits = found_ids(hub, "three hops without progress")
    assert f"{RESEARCH_ID}#loop-guards" in hits or RESEARCH_ID in hits, hits

    # carol's grant stops at public: the research note, at the project's label, is hidden from her; the guide is not
    assert call_tool(hub, "kg_node", {"id": RESEARCH_ID}, member="carol").json()["isError"]
    assert not any(hit.startswith(RESEARCH_ID) for hit in found_ids(hub, "three hops without progress", "carol"))
    assert node(hub, GUIDE_ID, member="carol")["label"]["level"] == "public"

    save(hub, RESEARCH, "Budgets and loops", BODY)  # the same content: no version, no build
    assert queued(hub) == 0
    assert hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice).status_code == 202
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    same = build_rows(hub)[0]  # the documents give the same items: the build before's artifact
    assert same["id"] != built["id"] and same["content_hash"] == built["content_hash"]
    assert same["artifact_reused_from"] == built["id"]
    changed = BODY.replace("three hops without progress", "five tool calls that change nothing")
    assert save(hub, RESEARCH, "Budgets and loops", changed, "dave")["version"] == 2
    assert queued(hub) == 1
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    again = node(hub, RESEARCH_ID)
    assert (again["props"]["version"], again["props"]["author"]) == (2, "dave")
    hits = found_ids(hub, "five tool calls that change nothing")
    assert f"{RESEARCH_ID}#loop-guards" in hits or RESEARCH_ID in hits, hits
    assert not any(hit.startswith(RESEARCH_ID) for hit in found_ids(hub, "three hops without progress"))

    off = hub.client.post(f"/v1/projects/{PROJECT}/plugins/docs/disable", headers=hub.owner)
    assert off.status_code == 200, off.text
    assert hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice).status_code == 202
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert build_rows(hub)[0]["status"] == "succeeded"
    for gone in (RESEARCH_ID, GUIDE_ID):  # the feature's documents left the graph with it
        assert call_tool(hub, "kg_node", {"id": gone}).json()["isError"], gone
