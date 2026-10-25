"""Push notifications on the hub (plan hub-mobile-phase-0, criteria a1 and a4): a member's push devices, the public
config a browser subscribes with, the deliveries a decision asked of a member gets on each of their devices through
the outbox, Web Push encrypted (RFC 8291) and signed (RFC 8292) for a fake push service, a device the service no longer
knows removed alone, and what a push carries: for a restricted project or a member who hides details, only the kind
of thing, the project and the link; never a decision's context or its files; and within 4 KB.

The fake push service is an httpx MockTransport handed to the sender: it decrypts each push with the browser's key
pair, as a browser would, with HKDF of ``cryptography`` rather than the hub's code."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient
from sqlalchemy import extract, func, select, update

from evo_agents.hub import push_payload, tables
from evo_agents.hub.server import notifications, push_senders, webpush
from evo_agents.hub.server.app import create_app
from tests.hub.live import sql
from tests.hub.test_decisions import QUESTION, ask
from tests.hub.test_notifications import PUSHED, notice
from tests.hub.test_plan_runs import plan_body, push, started
from tests.hub.test_plans import registration
from tests.hub.test_runs import OWNER, PROJECT, members

HUB_URL = "https://hub.test"
DEVICES = tables.push_devices
DELIVERIES = tables.notification_deliveries
CONTEXT_SECRET = "context-only-SECRET-7731"  # what a decision's context says, and no push may carry
RFC_8291 = {  # the example of RFC 8291, appendix A
    "plaintext": b"When I grow up, I want to be a watermelon",
    "sender_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "p256dh": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "auth": "BTBZMqHH6r4Tts7J_aSIgg",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    "body": "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wE"
    "qKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN",
}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def point(key) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_key() -> str:
    """A fresh VAPID private key as EVO_HUB_VAPID_PRIVATE_KEY takes it."""
    return b64(ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value.to_bytes(32, "big"))


VAPID_KEY = vapid_key()


@dataclass
class Browser:
    """A browser's push subscription, with the private key only the browser has."""

    endpoint: str
    key: ec.EllipticCurvePrivateKey
    auth: bytes

    def subscription(self) -> dict:
        keys = {"p256dh": b64(point(self.key.public_key())), "auth": b64(self.auth)}
        return {"endpoint": self.endpoint, "expirationTime": None, "keys": keys}

    def body(self, label: str | None = None) -> dict:
        found = {"kind": "webpush", "subscription": self.subscription()}
        return found if label is None else {**found, "label": label}

    def decrypt(self, body: bytes) -> dict:
        """The payload of a push, read as RFC 8291 says a browser reads it."""
        salt, size, length = body[:16], int.from_bytes(body[16:20], "big"), body[20]
        sender, record = body[21 : 21 + length], body[21 + length :]
        assert size == 4096 and len(body) <= 4096
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender))
        info = b"WebPush: info\x00" + point(self.key.public_key()) + sender
        ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=self.auth, info=info).derive(shared)
        cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
        plain = AESGCM(cek).decrypt(nonce, record, None).rstrip(b"\x00")
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


def browser(name: str, host: str = "fcm.googleapis.com") -> Browser:
    return Browser(
        f"https://{host}/fcm/send/{name}-{b64(os.urandom(6))}", ec.generate_private_key(ec.SECP256R1()), os.urandom(16)
    )


class PushService:
    """A fake push service: records each push and answers 201, or what ``answers`` says for an endpoint."""

    def __init__(self):
        self.pushes: list[httpx.Request] = []
        self.answers: dict[str, tuple[int, dict]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.pushes.append(request)
        status, headers = self.answers.get(str(request.url), (201, {"Location": f"{request.url}/messages/1"}))
        return httpx.Response(status, headers=headers, text="" if status < 300 else "nope")

    def to(self, found: Browser) -> list[httpx.Request]:
        return [request for request in self.pushes if str(request.url) == found.endpoint]


@pytest.fixture
def service(monkeypatch) -> PushService:
    fake = PushService()
    monkeypatch.setattr(push_senders.WebPushChannel, "transport", httpx.MockTransport(fake))
    return fake


def make_client(hub_db, tmp_path, github, **changes):
    config = live.hub_config(hub_db, tmp_path, github, public_url=HUB_URL, **changes)
    return TestClient(create_app(config), base_url=HUB_URL)


@pytest.fixture
def client(hub_db, tmp_path, github):
    with make_client(hub_db, tmp_path, github, vapid_private_key=VAPID_KEY) as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    headers = members(client, github)
    push(client, headers["owner"], plan_body())
    return headers


def deliver(client) -> dict:
    app = client.app
    return client.portal.call(lambda: notifications.deliver_notifications(app.state.engine, config=app.state.config))


def add(client, headers, body: dict):
    return client.post("/v1/me/push/devices", json=body, headers=headers)


def added(client, headers, body: dict) -> dict:
    response = add(client, headers, body)
    assert response.status_code == 201, response.text
    return response.json()


def devices(client, headers) -> list[dict]:
    response = client.get("/v1/me/push/devices", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["devices"]


def apns(token: str = "ab" * 32) -> dict:
    return {"kind": "apns", "token": token, "topic": "org.example.hub", "environment": "sandbox", "label": "iPhone"}


def fcm(token: str = "fcm-token-" + "x" * 140) -> dict:
    return {"kind": "fcm", "token": token, "label": "Pixel"}


def audited(db) -> list[tuple]:
    """(action, target) of each audit row of push devices, oldest first."""
    audit = tables.audit
    return sql(
        db, select(audit.c.action, audit.c.target).where(audit.c.action.like("push_device.%")).order_by(audit.c.id)
    )


def device_deliveries(db) -> list[tuple]:
    """(device kind, state, attempts, last_error) of each delivery to a device, oldest first."""
    query = (
        select(DEVICES.c.kind, DELIVERIES.c.state, DELIVERIES.c.attempts, DELIVERIES.c.last_error)
        .join_from(DELIVERIES, DEVICES, DEVICES.c.id == DELIVERIES.c.device_id)
        .order_by(DELIVERIES.c.id)
    )
    return sql(db, query)


def asked_decision(client, hub, **extra) -> tuple[dict, dict]:
    worker, run = started(client, hub)
    response = ask(client, worker, run["id"], **extra)
    assert response.status_code == 201, response.text
    return worker, response.json()


def clear(client, hub, clearance: dict) -> None:
    """The project's hub sink cleared for ``clearance``."""
    sinks = [
        {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal"}},
        {"id": "hub", "kind": "hub", "clearance": clearance},
    ]
    response = client.put(f"/v1/projects/{PROJECT}", json=registration(sinks), headers=hub["admin"])
    assert response.status_code == 200, response.text


def unrestrict(client, hub) -> None:
    """The project's hub sink cleared below customer and domestic-only: what Telegram gets whole."""
    clear(client, hub, {"level": "internal", "location": "any"})


def restrict(client, hub) -> None:
    """The project's hub sink cleared for customer: what Telegram keeps minimal."""
    clear(client, hub, {"level": "customer", "location": "any"})


def verify_vapid(request: httpx.Request) -> dict:
    """The claims of a push's VAPID JWT, once its ES256 signature checks with the key it names."""
    scheme, _, fields = request.headers["authorization"].partition(" ")
    assert scheme == "vapid"
    said = dict(part.strip().split("=", 1) for part in fields.split(","))
    head, claims, signature = said["t"].split(".")
    assert json.loads(unb64(head)) == {"typ": "JWT", "alg": "ES256"}
    raw = unb64(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(said["k"]))
    public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # InvalidSignature otherwise
    assert said["k"] == webpush.public_key(VAPID_KEY)
    return json.loads(unb64(claims))


# The config a browser subscribes with


def test_push_config_needs_no_sign_in_and_gives_the_vapid_key_and_the_senders_on(client):
    response = client.get("/v1/push/config")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "vapid_public_key": webpush.public_key(VAPID_KEY),
        "senders": {"webpush": True, "apns": False, "fcm": False},
    }
    assert len(unb64(response.json()["vapid_public_key"])) == 65


def test_without_a_vapid_key_web_push_is_off_and_the_rest_of_the_outbox_runs(hub_db, tmp_path, github, monkeypatch):
    sent = []

    class FakeChannel(notifications.Channel):
        async def send(self, notification, config):
            sent.append(notification.id)

    monkeypatch.setitem(notifications.CHANNELS, "fake", FakeChannel)
    with make_client(hub_db, tmp_path, github) as client:
        assert client.get("/v1/push/config").json() == {
            "vapid_public_key": None,
            "senders": {"webpush": False, "apns": False, "fcm": False},
        }
        hub = members(client, github)
        push(client, hub["owner"], plan_body())
        device = added(client, hub["owner"], browser("off").body())
        owner_id = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0][0]
        sql(hub_db, tables.notification_channels.insert().values(user_id=owner_id, kind="fake", config={}))
        asked_decision(client, hub)
        assert deliver(client) == {"delivered": 2, "retried": 0, "failed": 1}
        assert device_deliveries(hub_db) == [
            ("webpush", "failed", 1, "Undeliverable: Web Push is off on this hub: set EVO_HUB_VAPID_PRIVATE_KEY")
        ]
        assert len(sent) == 1
        test = client.post(f"/v1/me/push/devices/{device['id']}/test", headers=hub["owner"])
        assert test.status_code == 503 and "EVO_HUB_VAPID_PRIVATE_KEY" in test.json()["message"]


# A member's devices


def test_a_member_adds_lists_and_removes_devices_of_each_kind(client, hub, hub_db):
    laptop = browser("laptop")
    first = added(client, hub["owner"], laptop.body("Laptop Chrome"))
    phone = added(client, hub["owner"], apns())
    android = added(client, hub["owner"], fcm())
    assert (first["kind"], first["label"], first["last_sent_at"], first["this_session"]) == (
        "webpush",
        "Laptop Chrome",
        None,
        True,
    )
    listed = devices(client, hub["owner"])
    assert [(d["id"], d["kind"], d["label"]) for d in listed] == [
        (android["id"], "fcm", "Pixel"),
        (phone["id"], "apns", "iPhone"),
        (first["id"], "webpush", "Laptop Chrome"),
    ]
    assert set(listed[0]) == {"id", "kind", "label", "created_at", "last_sent_at", "this_session"}
    assert devices(client, hub["other"]) == []
    kept = sql(hub_db, select(DEVICES.c.kind, DEVICES.c.address, DEVICES.c.params).order_by(DEVICES.c.id))
    assert kept[0] == ("webpush", laptop.endpoint, laptop.subscription()["keys"])
    assert kept[1] == ("apns", "ab" * 32, {"topic": "org.example.hub", "environment": "sandbox"})
    # another member cannot remove it, nor can anyone twice
    assert client.delete(f"/v1/me/push/devices/{phone['id']}", headers=hub["other"]).status_code == 404
    assert client.delete(f"/v1/me/push/devices/{phone['id']}", headers=hub["owner"]).status_code == 204
    assert client.delete(f"/v1/me/push/devices/{phone['id']}", headers=hub["owner"]).status_code == 404
    assert [d["id"] for d in devices(client, hub["owner"])] == [android["id"], first["id"]]
    rows = audited(hub_db)
    assert [row[0] for row in rows] == ["push_device.add"] * 3 + ["push_device.remove"]
    assert rows[-1][1] == f"push_device:{phone['id']} kind=apns user={OWNER} by=member"
    assert not any(laptop.endpoint in row[1] or "ab" * 32 in row[1] for row in rows)


def test_adding_a_device_again_refreshes_it_and_another_member_who_adds_it_takes_it_over(client, hub, hub_db):
    laptop = browser("shared")
    first = added(client, hub["owner"], laptop.body("Mine"))
    again = added(client, hub["owner"], laptop.body())
    assert again["id"] == first["id"] and again["label"] == "Mine"
    assert len(devices(client, hub["owner"])) == 1
    taken = added(client, hub["other"], laptop.body("Theirs now"))
    assert devices(client, hub["owner"]) == []
    assert [(d["id"], d["label"]) for d in devices(client, hub["other"])] == [(taken["id"], "Theirs now")]
    assert sql(hub_db, select(func.count()).select_from(DEVICES)) == [(1,)]


def test_a_device_of_a_sign_in_that_ended_is_not_listed_and_goes_when_another_is_added(client, hub, hub_db):
    old = added(client, hub["owner"], browser("old").body())
    sql(hub_db, update(tables.tokens).values(revoked_at=func.now()).where(tables.tokens.c.id == token_of(hub_db, old)))
    fresh = live.insert_token(hub_db, OWNER)
    headers = live.bearer(fresh)
    assert devices(client, headers) == []
    added(client, headers, browser("new").body())
    assert [row[0] for row in sql(hub_db, select(DEVICES.c.id))] != [old["id"]]
    assert sql(hub_db, select(func.count()).select_from(DEVICES)) == [(1,)]


def token_of(db, device: dict) -> int:
    return sql(db, select(DEVICES.c.token_id).where(DEVICES.c.id == device["id"]))[0][0]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "webpush", "subscription": {"endpoint": "http://fcm.googleapis.com/fcm/send/x"}},
        "endpoint:https://example.org/push/x",  # not a push service
        "endpoint:https://10.0.0.7/push/x",
        "endpoint:https://fcm.googleapis.com:8443/fcm/send/x",
        "endpoint:https://user:pw@fcm.googleapis.com/fcm/send/x",
        "endpoint:https://fcm.googleapis.com.evil.example/fcm/send/x",
        "p256dh:" + b64(b"\x04" + b"\x01" * 64),  # not a point of P-256
        "p256dh:" + b64(b"\x05" * 33),
        "auth:" + b64(b"\x07" * 8),
        "label:two\nlines",
        {**apns(), "token": "not-hex"},
        {**apns(), "environment": "development"},
        {**apns(), "topic": ".starts-with-a-dot"},
        {**fcm(), "token": "short"},
        {"kind": "mail", "token": "x" * 40},
    ],
)
def test_a_device_the_route_does_not_take_gets_422(client, hub, hub_db, body):
    if isinstance(body, str):
        field, _, value = body.partition(":")
        sent = browser("bad").body()
        if field == "endpoint":
            sent["subscription"]["endpoint"] = value
        elif field == "label":
            sent["label"] = value
        else:
            sent["subscription"]["keys"][field] = value
        body = sent
    response = add(client, hub["owner"], body)
    assert response.status_code == 422, response.text
    assert sql(hub_db, select(func.count()).select_from(DEVICES)) == [(0,)]


def test_the_endpoints_of_the_push_services_of_browsers_are_taken():
    for host in (
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
        "wns2-par02p.notify.windows.com",
    ):
        assert webpush.endpoint_problem(f"https://{host}/x/y") is None, host


def test_a_member_has_at_most_twenty_devices(client, hub):
    for n in range(20):
        added(client, hub["owner"], fcm(f"fcm-token-{n:03d}-" + "y" * 40))
    response = add(client, hub["owner"], fcm("fcm-token-one-more-" + "z" * 40))
    assert response.status_code == 409 and "20 push devices" in response.json()["message"]
    assert add(client, hub["owner"], fcm("fcm-token-000-" + "y" * 40)).status_code == 201  # one of them, refreshed


def test_the_device_routes_need_a_sign_in(client):
    assert client.get("/v1/me/push/devices").status_code == 401
    assert client.post("/v1/me/push/devices", json=apns()).status_code == 401


# Deliveries through the outbox


def test_a_decision_asked_of_a_member_goes_to_each_of_their_devices_encrypted_and_signed(client, hub, hub_db, service):
    unrestrict(client, hub)
    laptop, desktop = browser("laptop"), browser("desktop", host="updates.push.services.mozilla.com")
    for found in (laptop, desktop):
        added(client, hub["owner"], found.body())
    added(client, hub["owner"], apns())
    stranger = browser("someone-else")
    added(client, hub["other"], stranger.body())
    _, decision = asked_decision(client, hub)
    assert deliver(client) == {"delivered": 3, "retried": 0, "failed": 1}
    # the web's, then one on each device of the person asked: APNs is off on this hub (tests/hub/test_push_native.py)
    assert device_deliveries(hub_db) == [
        ("webpush", "delivered", 1, None),
        ("webpush", "delivered", 1, None),
        (
            "apns",
            "failed",
            1,
            "Undeliverable: APNs is off on this hub: set EVO_HUB_APNS_KEY_ID, EVO_HUB_APNS_TEAM_ID, "
            "EVO_HUB_APNS_PRIVATE_KEY",
        ),
    ]
    assert service.to(stranger) == []
    for found in (laptop, desktop):
        (request,) = service.to(found)
        headers = request.headers
        assert (headers["content-encoding"], headers["content-type"]) == ("aes128gcm", "application/octet-stream")
        assert (headers["ttl"], headers["urgency"]) == ("86400", "high")
        claims = verify_vapid(request)
        origin = found.endpoint.split("/fcm/")[0]
        assert claims["aud"] == origin and claims["sub"] == HUB_URL
        assert time.time() < claims["exp"] <= time.time() + 24 * 3600
        shown = found.decrypt(request.content)
        assert shown == {
            "title": f"Run #{decision['run_id']} asks: {QUESTION}",
            "body": f"{QUESTION}\nRecommended: Move to Postgres",
            "url": f"{HUB_URL}/inbox?decision={decision['id']}",
            "tag": shown["tag"],
            "kind": "decision",
            "project": PROJECT,
        }
    external = sql(hub_db, select(DELIVERIES.c.external_id).where(DELIVERIES.c.device_id.is_not(None)))
    assert sorted(row[0] for row in external if row[0]) == sorted(f"{b.endpoint}/messages/1" for b in (laptop, desktop))
    sent_at = {d["kind"]: d["last_sent_at"] for d in devices(client, hub["owner"])}
    assert sent_at["webpush"] is not None and sent_at["apns"] is None


def test_a_notice_of_the_brief_tier_is_not_pushed(client, hub, hub_db, service):
    added(client, hub["owner"], browser("laptop").body())
    worker, run = started(client, hub)
    assert notice(client, worker, run["id"], **PUSHED).status_code == 201
    deliver(client)
    assert device_deliveries(hub_db) == [] and service.pushes == []


def test_a_410_removes_only_that_device_a_429_waits_and_a_500_is_tried_again(client, hub, hub_db, service):
    gone, busy, broken, fine = (browser(name) for name in ("gone", "busy", "broken", "fine"))
    ids = {found.endpoint: added(client, hub["owner"], found.body())["id"] for found in (gone, busy, broken, fine)}
    service.answers[gone.endpoint] = (410, {})
    service.answers[busy.endpoint] = (429, {"Retry-After": "120"})
    service.answers[broken.endpoint] = (500, {})
    asked_decision(client, hub)
    assert deliver(client) == {"delivered": 2, "retried": 2, "failed": 1}
    assert {d["id"] for d in devices(client, hub["owner"])} == {ids[f.endpoint] for f in (busy, broken, fine)}
    rows = sql(
        hub_db,
        select(DELIVERIES.c.device_id, DELIVERIES.c.state, DELIVERIES.c.attempts, DELIVERIES.c.last_error).where(
            DELIVERIES.c.device_id.is_not(None)
        ),
    )
    by_device = {row[0]: row[1:] for row in rows}
    assert ids[gone.endpoint] not in by_device  # its deliveries went with it
    assert by_device[ids[busy.endpoint]][:2] == ("pending", 0)
    assert by_device[ids[broken.endpoint]][:2] == ("pending", 1)
    assert "500" in by_device[ids[broken.endpoint]][2]
    assert by_device[ids[fine.endpoint]][:2] == ("delivered", 1)
    wait = sql(
        hub_db,
        select(extract("epoch", DELIVERIES.c.next_at - func.now())).where(DELIVERIES.c.device_id == ids[busy.endpoint]),
    )[0][0]
    assert 100 < wait <= 120
    removed = [row[1] for row in audited(hub_db) if row[0] == "push_device.remove"]
    assert removed == [f"push_device:{ids[gone.endpoint]} kind=webpush user={OWNER} by=hub"]


def test_a_device_whose_sign_in_ended_before_the_push_went_gets_nothing_and_goes(client, hub, hub_db, service):
    found = browser("signed-out")
    device = added(client, hub["owner"], found.body())
    asked_decision(client, hub)
    sql(
        hub_db,
        update(tables.tokens).values(revoked_at=func.now()).where(tables.tokens.c.id == token_of(hub_db, device)),
    )
    deliver(client)
    assert service.pushes == []
    assert sql(hub_db, select(func.count()).select_from(DEVICES)) == [(0,)]
    removed = [row[1] for row in audited(hub_db) if row[0] == "push_device.remove"]
    assert removed == [f"push_device:{device['id']} kind=webpush user={OWNER} by=hub"]


def test_a_member_without_a_grant_now_gets_no_push_and_keeps_the_device(client, hub, hub_db, service):
    added(client, hub["owner"], browser("laptop").body())
    asked_decision(client, hub)
    assert client.delete(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", headers=hub["admin"]).status_code == 204
    deliver(client)
    assert service.pushes == []
    assert device_deliveries(hub_db) == [("webpush", "failed", 1, notifications.NO_GRANT)]
    assert sql(hub_db, select(func.count()).select_from(DEVICES)) == [(1,)]


# A test push


def test_a_test_push_reaches_the_device_and_one_the_service_no_longer_knows_is_removed(client, hub, hub_db, service):
    laptop, old = browser("laptop"), browser("old")
    device = added(client, hub["owner"], laptop.body())
    stale = added(client, hub["owner"], old.body())
    phone = added(client, hub["owner"], apns())
    response = client.post(f"/v1/me/push/devices/{device['id']}/test", headers=hub["owner"])
    assert response.status_code == 200, response.text
    assert response.json() == {"sent": True, "removed": False, "error": None}
    (request,) = service.to(laptop)
    assert laptop.decrypt(request.content) == {
        "title": "Test notification",
        "body": "Push notifications from the hub reach this device.",
        "url": f"{HUB_URL}/settings/notifications",
        "tag": "evo-test",
        "kind": "test",
        "project": None,
    }
    assert next(d for d in devices(client, hub["owner"]) if d["id"] == device["id"])["last_sent_at"] is not None
    service.answers[old.endpoint] = (404, {})
    gone = client.post(f"/v1/me/push/devices/{stale['id']}/test", headers=hub["owner"]).json()
    assert (gone["sent"], gone["removed"]) == (False, True) and "404" in gone["error"]
    assert stale["id"] not in {d["id"] for d in devices(client, hub["owner"])}
    assert client.post(f"/v1/me/push/devices/{device['id']}/test", headers=hub["other"]).status_code == 404
    off = client.post(f"/v1/me/push/devices/{phone['id']}/test", headers=hub["owner"])
    assert off.status_code == 503 and "apns" in off.json()["message"]


# What a push carries (criterion a4)


def test_redact_a_restricted_project_push_carries_only_the_kind_the_project_and_the_link(client, hub, service):
    laptop = browser("laptop")
    added(client, hub["owner"], laptop.body())
    restrict(client, hub)
    _, decision = asked_decision(client, hub, context=f"## Why\n{CONTEXT_SECRET}")
    deliver(client)
    (request,) = service.to(laptop)
    shown = laptop.decrypt(request.content)
    assert shown == {
        "title": "Decision needed",
        "body": PROJECT,
        "url": f"{HUB_URL}/inbox?decision={decision['id']}",
        "tag": shown["tag"],
        "kind": "decision",
        "project": PROJECT,
    }
    assert QUESTION not in json.dumps(shown)


def test_redact_a_member_who_hides_details_gets_only_the_kind_the_project_and_the_link(client, hub, service):
    unrestrict(client, hub)
    laptop = browser("laptop")
    added(client, hub["owner"], laptop.body())
    chosen = client.put("/v1/me/notification-settings", json={"hide_details": True}, headers=hub["owner"])
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["hide_details"] is True
    assert client.get("/v1/me/notification-settings", headers=hub["owner"]).json()["hide_details"] is True
    assert client.get("/v1/me/notification-settings", headers=hub["other"]).json()["hide_details"] is False
    asked_decision(client, hub)
    deliver(client)
    (request,) = service.to(laptop)
    shown = laptop.decrypt(request.content)
    assert (shown["title"], shown["body"], shown["project"]) == ("Decision needed", PROJECT, PROJECT)
    assert QUESTION not in json.dumps(shown)


def test_redact_no_push_carries_a_decisions_context_or_its_files(client, hub, service):
    unrestrict(client, hub)
    laptop = browser("laptop")
    added(client, hub["owner"], laptop.body())
    asked_decision(client, hub, context=f"## Why\nThe answer depends on {CONTEXT_SECRET}.")
    deliver(client)
    (request,) = service.to(laptop)
    shown = laptop.decrypt(request.content)
    assert CONTEXT_SECRET not in json.dumps(shown)
    assert set(shown) == {"title", "body", "url", "tag", "kind", "project"}


def outgoing(**changes) -> SimpleNamespace:
    found = {
        "id": 7,
        "kind": "decision",
        "notice_kind": None,
        "project": "demo",
        "decision_id": 3,
        "title": "Run #4 asks: which?",
        "body": "The body a channel may show.",
        "link": "/inbox?decision=3",
        "extra": {"restricted": False, "hide_details": False, "decision": {"question": "Which?", "options": []}},
    }
    return SimpleNamespace(**{**found, **changes})


def test_payload_fails_closed_without_a_prepare_and_names_the_kind_of_a_notice():
    for extra in (None, {}, {"restricted": True}, {"restricted": False, "hide_details": True}):
        shown = push_payload.payload(outgoing(extra=extra), HUB_URL)
        assert (shown["title"], shown["body"]) == ("Decision needed", "demo")
    failed = push_payload.payload(
        outgoing(kind="notice", notice_kind="run_failed", decision_id=None, extra={"restricted": True}), HUB_URL
    )
    assert (failed["title"], failed["body"]) == ("Run failed", "demo")
    full = push_payload.payload(outgoing(kind="notice", notice_kind="run_failed", decision_id=None), None)
    assert (full["title"], full["body"], full["url"]) == (
        "Run #4 asks: which?",
        "Run failed in demo",
        "/inbox?decision=3",
    )
    assert push_payload.payload(outgoing(kind="proposal", extra=None), HUB_URL)["title"] == "Proposal to review"


def test_payload_fits_four_kilobytes_whatever_the_title_and_the_question():
    question = "é" * 20_000
    huge = outgoing(title="t" * 5000, extra={"restricted": False, "decision": {"question": question, "options": []}})
    shown = push_payload.payload(huge, HUB_URL)
    assert push_payload.size(shown) <= push_payload.MAX_PAYLOAD_BYTES < 4096
    assert shown["body"].endswith("...") and shown["url"] == f"{HUB_URL}/inbox?decision=3"
    wide = outgoing(title="\n" * 4000, extra={"restricted": False, "decision": {"question": "\x01" * 9000}})
    assert push_payload.size(push_payload.payload(wide, HUB_URL)) <= push_payload.MAX_PAYLOAD_BYTES
    found = browser("big")
    body = webpush.encrypt(push_payload.encoded(shown), found.subscription()["keys"]["p256dh"], b64(found.auth))
    assert len(body) <= 4096 and found.decrypt(body) == shown


# The cryptography


def test_encryption_gives_the_example_of_rfc_8291():
    sender_private = int.from_bytes(unb64(RFC_8291["sender_private"]), "big")
    sender = ec.derive_private_key(sender_private, ec.SECP256R1())
    body = webpush.encrypt(
        RFC_8291["plaintext"],
        RFC_8291["p256dh"],
        RFC_8291["auth"],
        salt=unb64(RFC_8291["salt"]),
        sender=sender,
    )
    assert b64(body) == RFC_8291["body"]


def test_encryption_refuses_a_payload_over_one_record_and_keys_that_are_not_keys():
    found = browser("keys")
    keys = found.subscription()["keys"]
    with pytest.raises(ValueError):
        webpush.encrypt(b"x" * (webpush.MAX_PLAINTEXT + 1), keys["p256dh"], keys["auth"])
    assert found.decrypt(webpush.encrypt(b'{"a":1}' + b" " * (webpush.MAX_PLAINTEXT - 7), keys["p256dh"], keys["auth"]))
    for p256dh, auth in (
        (keys["p256dh"][:-4], keys["auth"]),
        (keys["p256dh"], keys["auth"] + "AA"),
        ("not base64!", keys["auth"]),
    ):
        with pytest.raises(ValueError):
            webpush.encrypt(b"{}", p256dh, auth)


def test_the_vapid_header_is_an_es256_jwt_for_the_endpoints_origin():
    request = httpx.Request("POST", "https://web.push.apple.com/QGuq?x=1")
    request.headers["authorization"] = webpush.vapid_authorization(
        "https://web.push.apple.com/QGuq?x=1", VAPID_KEY, "mailto:ops@example.org", now=1_800_000_000
    )
    claims = verify_vapid(request)
    assert claims == {
        "aud": "https://web.push.apple.com",
        "exp": 1_800_000_000 + 12 * 3600,
        "sub": "mailto:ops@example.org",
    }
