"""evo_agents.hub.api: the paths of the hub's API, written once for the synchronous and the asynchronous client."""

import asyncio

from evo_agents.hub import api
from evo_agents.hub.api import paths


class SyncHub:
    url = "https://hub.example"

    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"path": path}


class AsyncHub(SyncHub):
    async def call(self, method, path, body=None):
        return super().call(method, path, body)


class Plans(api.ProjectResource):
    collection = "plans"


def test_route_quotes_each_part_whole_and_drops_a_query_of_none():
    assert api.route("worker/runs", 7, "steps", "a/b", limit=None, q="x y") == "/v1/worker/runs/7/steps/a%2Fb?q=x+y"
    assert paths.project("my proj") == "/v1/projects/my%20proj"


def test_a_resource_serves_the_sync_and_the_async_client_alike():
    sync = Plans(SyncHub(), "p/1")
    assert sync.call("GET", "x", limit=3) == {"path": "/v1/projects/p%2F1/plans/x?limit=3"}
    hub = AsyncHub()
    found = asyncio.run(Plans(hub, "p/1").call("POST", "x", body={"a": 1}))
    assert found == {"path": "/v1/projects/p%2F1/plans/x"}
    assert hub.calls == [("POST", "/v1/projects/p%2F1/plans/x", {"a": 1})]


def test_the_kinds_of_resource():
    class Tasks(api.FeatureResource):
        feature = "tasks"

    class Builds(api.KgResource):
        collection = "builds"

    class Tokens(api.HubRoutes):
        base = "tokens"

    hub = SyncHub()
    assert Tasks(hub, "p").path("t", 1, query=[("s", "a"), ("s", "b")]) == "/v1/x/tasks/p/t/1?s=a&s=b"
    assert Builds(hub, "p").path(3) == "/v1/kg/p/builds/3"
    assert Tokens(hub).path == "/v1/tokens"
    assert api.WorkerRun(hub, "12").path("browser-profile", 4, base_version=2) == (
        "/v1/worker/runs/12/browser-profile/4?base_version=2"
    )
    assert api.is_route("/v1/worker/claim?x=1", "worker/claim")
    assert not api.is_route("/v1/worker/claims", "worker/claim")
