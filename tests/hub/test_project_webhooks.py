"""Outbound webhooks of a project (plan evo-creative-workspace, step 2, criterion a2; ``evo_agents.hub.webhook_events``
and ``evo_agents.hub.server.project_webhooks``).

The checks the step names: a project's admin alone sets its webhooks, whose secret is in the answers that make it
alone and is stored sealed; the events run.state, chat.message, task.result and curator.proposal reach the outbox of
the webhooks that take them, in the transaction that made them; a delivery is a POST signed with HMAC-SHA256 under
the secret, retried with a backoff while its receiver fails, and failed after MAX_ATTEMPTS tries or when its webhook
is turned off. A webhook's URL names a public host, and each try goes to an address of it that was checked public
(``evo_agents.hub.server.webhook_delivery``); a pass claims one delivery at a time."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from evo_agents import secret_patterns
from evo_agents.hub import tables, webhook_events
from evo_agents.hub.server import webhook_delivery
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub.live import sql
from tests.hub.test_app_session_auth import client, hub  # noqa: F401 (fixtures)
from tests.hub.test_curator import night  # noqa: F401 (a fixture)
from tests.hub.test_review_runs import held_review, proposed, review  # noqa: F401 (review: a fixture)
from tests.hub.test_runs import (
    OWNER,
    PROJECT,
    add_worker,
    audit_rows,
    claim,
    dispatched,
    members,
    web_client,  # noqa: F401 (a fixture)
)

URL = "https://hooks.lumenops.example/evo"
PUBLIC = "93.184.216.34"
ADDRESSES = {"hooks.lumenops.example": [PUBLIC]}
PRIVATE_URLS = (
    "https://127.0.0.1:5432/evo",
    "https://169.254.169.254/latest/meta-data",
    "https://10.0.0.5/evo",
    "https://[::1]/evo",
    "https://postgres/evo",
    "https://localhost/evo",
    "https://hooks.lumenops.internal/evo",
)
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}


@pytest.fixture
def admin(client, hub) -> dict:  # noqa: F811
    """The owner of ``hub``, made an admin of the project: the headers of a session that sets webhooks."""
    made = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=hub["admin"])
    assert made.status_code == 200, made.text
    return hub["owner"]


def created(client, headers, events=("run.state",), url: str = URL) -> dict:  # noqa: F811
    response = client.post(
        f"/v1/projects/{PROJECT}/webhooks", json={"url": url, "events": list(events)}, headers=headers
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def outbox(db, event: str | None = None) -> list[tuple]:
    d = tables.webhook_deliveries
    query = select(d.c.webhook_id, d.c.event, d.c.payload, d.c.state).order_by(d.c.id)
    return sql(db, query if event is None else query.where(d.c.event == event))


class Receiver:
    """A receiver of webhooks behind httpx.MockTransport: what it got, answering ``status``."""

    def __init__(self, status: int = 200):
        self.status = status
        self.got: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.answer)

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.got.append(request)
        return httpx.Response(self.status, text="the receiver's body, which the hub keeps none of")


def deliver(client, receiver: Receiver, addresses: dict | None = None) -> dict:  # noqa: F811
    """One pass of the deliveries, with the hosts resolving as ``addresses`` (ADDRESSES by default) says."""
    state, found = client.app.state, ADDRESSES if addresses is None else addresses

    async def resolve(host: str, port: int) -> list[str]:
        if host not in found:
            raise OSError("no such host")
        return list(found[host])

    return client.portal.call(
        lambda: webhook_delivery.deliver(state.engine, state.sealer, transport=receiver.transport, resolver=resolve)
    )


def due_now(db) -> None:
    d = tables.webhook_deliveries
    sql(db, update(d).values(next_at=func.now() - timedelta(seconds=1)).where(d.c.state == "pending"))


def run_state_delivery(client, hub, admin) -> tuple[dict, int]:  # noqa: F811
    """A webhook taking run.state, and a run of step 2 claimed by the owner's worker: (the webhook, the run)."""
    hook = created(client, admin)
    worker = add_worker(client, hub["owner"], "mac-mini")
    (run,) = dispatched(client, hub["owner"], [2])
    assert claim(client, worker)["id"] == run["id"]
    return hook, run["id"]


# Setting them


def test_an_admin_sets_webhooks_and_the_secret_is_given_once_and_kept_sealed(client, hub, admin, hub_db):  # noqa: F811
    hook = created(client, admin, events=("task.result", "run.state", "run.state"))
    assert hook["secret"].startswith("evx_") and hook["events"] == ["run.state", "task.result"]
    assert (hook["url"], hook["active"], hook["created_by"], hook["pending"], hook["failed"]) == (
        URL,
        True,
        OWNER,
        0,
        0,
    )
    listed = client.get(f"/v1/projects/{PROJECT}/webhooks", headers=admin).json()
    assert [item["id"] for item in listed] == [hook["id"]] and "secret" not in listed[0]
    assert hook["secret"] not in live.table_dump(hub_db)
    # a log line masks it, as every token of the hub
    assert hook["secret"] not in secret_patterns.HUB_TOKEN.sub(r"\g<head>***", f"secret={hook['secret']}")

    changed = client.patch(
        f"/v1/projects/{PROJECT}/webhooks/{hook['id']}",
        json={"events": ["chat.message"], "url": "https://hooks.lumenops.example/v2"},
        headers=admin,
    )
    assert changed.status_code == 200, changed.text
    assert (changed.json()["events"], changed.json()["url"]) == (["chat.message"], "https://hooks.lumenops.example/v2")
    assert "secret" not in changed.json()
    rotated = client.post(f"/v1/projects/{PROJECT}/webhooks/{hook['id']}/rotate-secret", headers=admin)
    assert rotated.status_code == 200 and rotated.headers["cache-control"] == "no-store"
    assert rotated.json()["secret"] != hook["secret"] and rotated.json()["secret"] not in live.table_dump(hub_db)
    assert client.delete(f"/v1/projects/{PROJECT}/webhooks/{hook['id']}", headers=admin).status_code == 204
    assert client.get(f"/v1/projects/{PROJECT}/webhooks", headers=admin).json() == []
    assert [row[:2] for row in audit_rows(hub_db, "webhook")] == [
        ("webhook.create", f"{PROJECT} webhook={hook['id']}"),
        ("webhook.update", f"{PROJECT} webhook={hook['id']}"),
        ("webhook.update", f"{PROJECT} webhook={hook['id']}"),
        ("webhook.delete", f"{PROJECT} webhook={hook['id']}"),
    ]
    assert "lumenops" not in str(audit_rows(hub_db, "webhook"))


def test_only_the_admin_sets_or_reads_them_and_a_bad_webhook_is_422(client, hub, admin, hub_db):  # noqa: F811
    path = f"/v1/projects/{PROJECT}/webhooks"
    body = {"url": URL, "events": ["run.state"]}
    for name in ("other", "reader"):  # a writer and a reader of the project
        assert client.post(path, json=body, headers=hub[name]).status_code == 403
        assert client.get(path, headers=hub[name]).status_code == 403
    assert client.post(path, json=body, headers=hub["stranger"]).status_code == 404
    for bad in (
        {"url": "http://hooks.lumenops.example/evo", "events": ["run.state"]},
        {"url": "https://hooks.lumenops.example/evo#frag", "events": ["run.state"]},
        {"url": URL, "events": []},
        {"url": URL, "events": ["plan.step"]},
        {"url": "https://user:pw@hooks.lumenops.example/evo", "events": ["run.state"]},
        *({"url": url, "events": ["run.state"]} for url in PRIVATE_URLS),
    ):
        assert client.post(path, json=bad, headers=admin).status_code == 422, bad
    assert sql(hub_db, select(func.count()).select_from(tables.project_webhooks)) == [(0,)]
    hook = created(client, admin)
    assert client.patch(f"{path}/{hook['id']}", json={"active": False}, headers=hub["other"]).status_code == 403
    for url in PRIVATE_URLS:
        assert client.patch(f"{path}/{hook['id']}", json={"url": url}, headers=admin).status_code == 422, url
    assert client.delete(f"{path}/{hook['id'] + 1}", headers=admin).status_code == 404


def test_a_hub_without_a_secrets_key_makes_no_webhook(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as keyless:
        headers = members(keyless, github)
        made = keyless.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
        assert made.status_code == 200, made.text
        body = {"url": URL, "events": ["run.state"]}
        refused = keyless.post(f"/v1/projects/{PROJECT}/webhooks", json=body, headers=headers["owner"])
        assert refused.status_code == 503 and "EVO_HUB_SECRETS_KEY" in refused.json()["message"]


# The events


def test_run_state_and_chat_message_reach_the_webhooks_that_take_them(client, hub, admin, hub_db):  # noqa: F811
    chats = created(client, admin, events=("chat.message",))
    off = created(client, admin, events=("run.state",))
    assert client.patch(f"/v1/projects/{PROJECT}/webhooks/{off['id']}", json={"active": False}, headers=admin)
    hook, run_id = run_state_delivery(client, hub, admin)
    stored = outbox(hub_db, "run.state")
    assert {row[0] for row in stored} == {hook["id"]}  # the one turned off takes none
    assert stored[0][2] == {
        "run_id": run_id,
        "run_kind": "step",
        "from": "queued",
        "to": "leased",
        "actor": "worker",
        "plan_id": "rollout",
        "step": "2",
    }
    made = client.post(f"/v1/projects/{PROJECT}/agents", json={"handle": "helper", "name": "Helper"}, headers=admin)
    assert made.status_code == 201, made.text
    chat = client.post("/v1/chats", json={"title": "Talk", "people": [], "agents": ["helper"]}, headers=admin)
    assert chat.status_code == 201, chat.text
    said = client.post(f"/v1/chats/{chat.json()['id']}/messages", json={"body": "hello there"}, headers=admin)
    assert said.status_code == 201, said.text
    (message,) = [row for row in outbox(hub_db, "chat.message") if row[0] == chats["id"]]
    assert message[2]["chat_id"] == chat.json()["id"] and "hello there" not in json.dumps(message[2])


def test_a_proposal_of_the_curator_reaches_the_webhooks_that_take_it(review, hub_db):  # noqa: F811
    ((project_id,),) = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))
    hooks = tables.project_webhooks
    row = {"project_id": project_id, "url": URL, "sealed_secret": {}}
    sql(hub_db, insert(hooks).values(**row, events=["curator.proposal"]))
    sql(hub_db, insert(hooks).values(**row, events=["run.state"]))
    run_id = held_review(review)
    made = proposed(review.client, review.worker, run_id)
    ((payload,),) = [(row[2],) for row in outbox(hub_db, "curator.proposal")]
    assert payload == {
        "proposal_id": made["id"],
        "run_id": run_id,
        "kind": "fix",
        "tier": made["tier"],
        "state": "open",
    }


# Their delivery


def test_a_delivery_is_signed_with_the_webhooks_secret(client, hub, admin, hub_db):  # noqa: F811
    hook, run_id = run_state_delivery(client, hub, admin)
    receiver = Receiver()
    assert deliver(client, receiver) == {"delivered": 1, "retry": 0, "failed": 0}
    (request,) = receiver.got
    assert (request.url.host, request.url.path, request.method) == (PUBLIC, "/evo", "POST")
    assert request.headers["host"] == "hooks.lumenops.example"
    assert request.extensions["sni_hostname"] == "hooks.lumenops.example"
    timestamp, sent = request.headers[webhook_events.TIMESTAMP_HEADER], request.content
    expected = hmac.new(hook["secret"].encode(), timestamp.encode() + b"." + sent, hashlib.sha256).hexdigest()
    assert request.headers[webhook_events.SIGNATURE_HEADER] == f"sha256={expected}"
    assert request.headers[webhook_events.EVENT_HEADER] == "run.state"
    body = json.loads(sent)
    assert (body["event"], body["project"], body["data"]["run_id"]) == ("run.state", PROJECT, run_id)
    assert request.headers[webhook_events.DELIVERY_HEADER] == str(body["id"])
    assert [row[3] for row in outbox(hub_db)] == ["delivered"]
    assert deliver(client, receiver) == {"delivered": 0, "retry": 0, "failed": 0}  # at least once, not again
    assert hook["secret"] not in live.table_dump(hub_db)


def test_a_failing_receiver_is_tried_again_with_a_backoff_then_failed(client, hub, admin, hub_db):  # noqa: F811
    hook, _ = run_state_delivery(client, hub, admin)
    receiver = Receiver(status=500)
    assert deliver(client, receiver) == {"delivered": 0, "retry": 1, "failed": 0}
    d = tables.webhook_deliveries
    query = select(d.c.state, d.c.attempts, d.c.last_status, d.c.last_error, d.c.next_at > func.now())
    assert sql(hub_db, query) == [("pending", 1, 500, "answered HTTP 500", True)]
    assert deliver(client, receiver) == {"delivered": 0, "retry": 0, "failed": 0}  # not due yet
    assert (
        webhook_events.backoff(1) < webhook_events.backoff(2) <= timedelta(seconds=webhook_events.BACKOFF_MAX_SECONDS)
    )
    for _ in range(webhook_events.MAX_ATTEMPTS - 1):
        due_now(hub_db)
        deliver(client, receiver)
    assert sql(hub_db, select(d.c.state, d.c.attempts)) == [("failed", webhook_events.MAX_ATTEMPTS)]
    assert len(receiver.got) == webhook_events.MAX_ATTEMPTS
    listed = client.get(f"/v1/projects/{PROJECT}/webhooks/{hook['id']}/deliveries", headers=admin).json()
    assert [(item["state"], item["last_status"]) for item in listed] == [("failed", 500)]
    assert "the receiver's body" not in live.table_dump(hub_db)


def test_a_webhook_turned_off_fails_what_it_left_pending(client, hub, admin, hub_db):  # noqa: F811
    hook, _ = run_state_delivery(client, hub, admin)
    off = client.patch(f"/v1/projects/{PROJECT}/webhooks/{hook['id']}", json={"active": False}, headers=admin)
    assert off.status_code == 200 and off.json()["pending"] == 1
    receiver = Receiver()
    assert deliver(client, receiver) == {"delivered": 0, "retry": 0, "failed": 1}
    assert receiver.got == [] and [row[3] for row in outbox(hub_db)] == ["failed"]


def again(db) -> None:
    """The pending deliveries due now, with no try yet: a test tries one more often than MAX_ATTEMPTS."""
    due_now(db)
    sql(db, update(tables.webhook_deliveries).values(attempts=0))


def test_a_host_that_resolves_to_a_private_address_is_not_called(client, hub, admin, hub_db):  # noqa: F811
    hook, _ = run_state_delivery(client, hub, admin)
    d, receiver = tables.webhook_deliveries, Receiver()
    for addresses in (["10.0.0.5"], ["169.254.169.254"], [PUBLIC, "127.0.0.1"], ["::ffff:192.168.0.1"]):
        again(hub_db)
        found = deliver(client, receiver, {"hooks.lumenops.example": addresses})
        assert found == {"delivered": 0, "retry": 1, "failed": 0}, addresses
        ((error,),) = sql(hub_db, select(d.c.last_error))
        assert error.startswith("refused: hooks.lumenops.example resolves to"), error
    again(hub_db)
    assert deliver(client, receiver, {}) == {"delivered": 0, "retry": 1, "failed": 0}  # it does not resolve
    h = tables.project_webhooks  # a URL stored before the check, or by hand, is checked again at each try
    for url in PRIVATE_URLS:
        sql(hub_db, update(h).values(url=url).where(h.c.id == hook["id"]))
        again(hub_db)
        assert deliver(client, receiver, {"postgres": ["10.0.0.5"]}) == {"delivered": 0, "retry": 1, "failed": 0}
    assert receiver.got == []
    sql(hub_db, update(h).values(url=URL).where(h.c.id == hook["id"]))
    again(hub_db)
    assert deliver(client, receiver) == {"delivered": 1, "retry": 0, "failed": 0}


def test_a_pass_claims_one_delivery_at_a_time_for_longer_than_a_try(client, hub, admin, hub_db):  # noqa: F811
    created(client, admin, url="https://hooks.lumenops.example/second")
    run_state_delivery(client, hub, admin)
    d, engine = tables.webhook_deliveries, client.app.state.engine
    due = select(func.count()).select_from(d).where(d.c.state == "pending", d.c.next_at <= func.now())
    ((pending,),) = sql(hub_db, due)
    assert pending >= 2
    first = client.portal.call(lambda: webhook_delivery._claim(engine))
    ((ahead,),) = sql(hub_db, select(d.c.next_at - func.now()).where(d.c.id == first.id))
    assert ahead > timedelta(seconds=webhook_delivery.TIMEOUT)  # a second pass cannot take it while it is tried
    assert sql(hub_db, due) == [(pending - 1,)]  # the others stay due for another pass
    second = client.portal.call(lambda: webhook_delivery._claim(engine))
    assert second.id != first.id
