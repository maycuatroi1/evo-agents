"""How a hub build, whose container has no ``claude``, gets entities from a run of the project's agent pool
(``evo_agents.hub.kg_entity_relay``): the build defers its items as requests, ``evo-agents hub kg entities`` answers
them on the run's login and posts them, and the next build reads those answers as the step's inbox. The hub's routes
are stood in for by a fake hub over the same functions, the blob store by a dict; the database is not needed."""

import hashlib
import json

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import kg_build, kg_coverage, kg_entities_cli
from evo_agents.hub import kg_entity_relay as relay
from evo_agents.kg import claude_code
from tests.kg.test_entities_claude_code import API_KEY, OAUTH, fake_claude, mapping  # noqa: F401
from tests.kg.test_extract_entities import entity_nodes, setup


@pytest.fixture
def kg_env(tmp_path, monkeypatch):
    home = tmp_path / "kg-home"
    monkeypatch.setenv("EVO_KG_HOME", str(home))
    return home


class Store:
    """The blob store's fetch over a dict of hash to bytes."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        self.objects[sha] = data
        return sha

    def fetch(self, key, sink, limit):
        sha = key.rsplit("/", 1)[-1]
        data = self.objects.get(sha)
        if data is None:
            return None
        sink.write(data[:limit])
        return hashlib.sha256(data[:limit]).hexdigest(), min(len(data), limit)


class Hub:
    """GET requests and POST answers as the routes of ``evo_agents.hub.server.kg_entities`` answer them."""

    def __init__(self, store: Store, requests: bytes):
        self.store, self.requests, self.answers, self.posts = store, requests, [], 0

    def call(self, method, path, body=None):
        rows = relay.parse_requests(self.requests)
        if method == "GET":
            assert path == "/v1/kg/alpha/entities/requests"
            return {"build_id": 1, "requests": rows}
        assert path == "/v1/kg/alpha/entities/answers"
        self.posts += 1
        kept = {k: v for k, v in relay.clean_answers(body["answers"]).items() if k in {r["key"] for r in rows}}
        data, _ = relay.encode_answers(kept)
        self.answers.append(self.store.put(data))
        return {"kept": len(kept), "ignored": len(body["answers"]) - len(kept), "queued": True}


def hub_build(project, store: Store, answers: list[str], monkeypatch, tmp_path):
    """A build as ``kg_build.build_in_cache`` runs it in the container: no claude, the answers blobs as the inbox."""
    monkeypatch.setenv(claude_code.BINARY_ENV, str(tmp_path / "no-claude-in-the-container"))
    outbox: list[dict] = []
    path, report = kg_build.build_graph(project, entity_inbox=relay.read_answers(store, answers), entity_outbox=outbox)
    return report, relay.write_requests(project.root, outbox)


def test_a_hub_build_defers_to_a_pool_run_whose_answers_the_next_build_states(
    tmp_path, kg_env, fake_claude, monkeypatch
):
    calls, _ = fake_claude
    script = claude_code.binary()
    project, _ = setup(tmp_path, kg_env, extra=mapping())
    store = Store()
    report, requests = hub_build(project, store, [], monkeypatch, tmp_path)
    assert report.ok and calls() == [] and entity_nodes(project) == []
    path, sha, size, count = requests
    assert count == 3 and report.coverage["entities"]["deferred"] == 3
    kept = kg_coverage.build_coverage(report.coverage, None, skipped=False)["entities"]
    assert kept["deferred"] == 3 and kept["model"] == "claude-haiku-5-5" and kept["sink"] == "llm"
    assert "Intro" not in json.dumps(kept), "the coverage the hub keeps names no text"

    monkeypatch.setenv(claude_code.BINARY_ENV, script)  # the pool run, which has claude and the lease of the login
    hub = Hub(store, store.objects.setdefault(sha, path.read_bytes()))
    done = kg_entities_cli.answer_all(hub, "alpha")
    assert done == {"project": "alpha", "requests": 3, "answered": 3, "kept": 3, "failed": 0, "stopped": None}
    assert len(calls()) == 3 and all(c["oauth"] and not c["api_key"] for c in calls())
    assert all(API_KEY.encode() not in data and OAUTH.encode() not in data for data in store.objects.values())

    after, left = hub_build(project, store, hub.answers, monkeypatch, tmp_path)
    cov = after.coverage["entities"]
    assert left is None and cov["answered_elsewhere"] == 3 and cov["deferred"] == 0 and cov["calls"] == 0
    assert {"entity:Concept:search", "entity:Concept:server"} <= {r[0] for r in entity_nodes(project)}
    assert len(calls()) == 3, "the build itself never runs claude"


def test_a_pool_run_that_hits_a_limit_posts_what_it_has_and_the_rest_waits(tmp_path, kg_env, fake_claude, monkeypatch):
    calls, set_mode = fake_claude
    script = claude_code.binary()
    project, _ = setup(tmp_path, kg_env, extra=mapping())
    store = Store()
    _, (path, sha, _, _) = hub_build(project, store, [], monkeypatch, tmp_path)
    monkeypatch.setenv(claude_code.BINARY_ENV, script)
    set_mode("limit-after-1")
    hub = Hub(store, path.read_bytes())
    done = kg_entities_cli.answer_all(hub, "alpha")
    assert done["answered"] == 1 and done["kept"] == 1 and done["stopped"] == "rate_limit" and hub.posts == 1
    after, left = hub_build(project, store, hub.answers, monkeypatch, tmp_path)
    assert after.coverage["entities"]["answered_elsewhere"] == 1 and left[3] == 2, "two items wait for the next run"


def test_answers_out_of_shape_or_of_another_blob_are_left_out(tmp_path):
    with pytest.raises(relay.RelayError):
        relay.clean_answers({"k": "not an object"})
    got = relay.clean_answers({"k": {"nodes": [{"name": "A"}, "x"], "usage": {"cost": 0.1, "token": "t"}}})
    assert got == {"k": {"nodes": [{"name": "A"}], "edges": [], "usage": {"cost": 0.1}}}
    store = Store()
    good = store.put(relay.encode_answers(got)[0])
    store.objects["f" * 64] = b"not json"
    assert relay.read_answers(store, ["0" * 64, "f" * 64, good]) == got
    assert relay.requests_of({"entity_requests": {"sha256": "a", "count": 2}}) == ("a", 2)
    assert relay.requests_of({}) is None and relay.write_requests(tmp_path, []) is None
