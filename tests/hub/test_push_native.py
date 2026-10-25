"""The senders of the hub's apps (plan hub-mobile-phase-0, criterion a2): APNs over HTTP/2 with an ES256 provider
token, FCM's HTTP v1 API with an OAuth token of a service account, each against a fake in this process, never Apple or
Google. A decision asked of a member reaches their iPhones and Android phones through the outbox; a token APNs answers
410, BadDeviceToken or Unregistered for, or FCM answers UNREGISTERED for, removes that device alone; a busy or broken
service is tried again; a sender without its variables is off, and GET /v1/push/config says so.

The fake APNs is an HTTP/2 server on 127.0.0.1 in a thread of its own (h2c with prior knowledge, since it has no
certificate): it speaks HTTP/2 alone, as APNs does, so a push it records went over HTTP/2. The fake Google is an httpx
MockTransport with the token endpoint, which checks the service account's signed assertion, and FCM's send method."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import h2.config
import h2.connection
import h2.events
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sqlalchemy import extract, func, select, update

from evo_agents.hub import push_payload
from evo_agents.hub.log import scrub
from evo_agents.hub.server import push_native
from tests.hub.live import sql
from tests.hub.test_decisions import QUESTION
from tests.hub.test_plan_runs import plan_body, push
from tests.hub.test_push import (
    CONTEXT_SECRET,
    DELIVERIES,
    HUB_URL,
    added,
    apns,
    asked_decision,
    audited,
    deliver,
    devices,
    fcm,
    make_client,
    restrict,
    unb64,
    unrestrict,
)
from tests.hub.test_runs import OWNER, PROJECT, members

KEY_ID, TEAM_ID = "KEY123ABCD", "TEAM98765Z"
APNS_KEY = ec.generate_private_key(ec.SECP256R1())
FCM_PROJECT = "northwind-hub"
FCM_EMAIL = "hub-push@northwind-hub.iam.gserviceaccount.com"
FCM_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pkcs8(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


APNS_CONFIG = {"apns_key_id": KEY_ID, "apns_team_id": TEAM_ID, "apns_private_key": pkcs8(APNS_KEY)}
FCM_CONFIG = {"fcm_project_id": FCM_PROJECT, "fcm_client_email": FCM_EMAIL, "fcm_private_key": pkcs8(FCM_KEY)}
TITLE_OF = "Run #{run} asks: " + QUESTION
BODY = f"{QUESTION}\nRecommended: Move to Postgres"


def verify_jwt(token: str, public_key) -> tuple[dict, dict]:
    """The header and the claims of ``token`` once its signature checks with ``public_key`` (ES256 or RS256)."""
    head, claims, signature = token.split(".")
    signed, raw = f"{head}.{claims}".encode(), unb64(signature)
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        public_key.verify(der, signed, ec.ECDSA(hashes.SHA256()))  # InvalidSignature otherwise
    else:
        public_key.verify(raw, signed, padding.PKCS1v15(), hashes.SHA256())
    return json.loads(unb64(head)), json.loads(unb64(claims))


# The fake APNs: HTTP/2 alone


@dataclass
class ApnsPush:
    headers: dict[str, str]
    body: bytes
    apns_id: str

    @property
    def path(self) -> str:
        return self.headers[":path"]

    def shown(self) -> dict:
        return json.loads(self.body)


class FakeApns:
    """An APNs of one environment: an HTTP/2 server on a port of 127.0.0.1, in a thread with its own event loop. It
    records each push and answers 200 with an apns-id, or (status, reason) as ``answers`` says for a device token."""

    def __init__(self):
        self.pushes: list[ApnsPush] = []
        self.answers: dict[str, tuple[int, str]] = {}
        self.errors: list[str] = []
        self.loop = asyncio.new_event_loop()
        started = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(started,), daemon=True)
        self.thread.start()
        assert started.wait(10), "the fake APNs did not start"

    def _run(self, started: threading.Event) -> None:
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(asyncio.start_server(self._serve, "127.0.0.1", 0))
        self.origin = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        started.set()
        self.loop.run_forever()

    def reset(self) -> None:
        self.pushes.clear()
        self.answers.clear()
        self.errors.clear()

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        streams: dict[int, dict] = {}
        try:
            while data := await reader.read(65536):
                for event in conn.receive_data(data):  # h2 raises on anything but HTTP/2
                    if isinstance(event, h2.events.RequestReceived):
                        streams[event.stream_id] = {"headers": dict(event.headers), "body": b""}
                    elif isinstance(event, h2.events.DataReceived):
                        streams[event.stream_id]["body"] += event.data
                        conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, h2.events.StreamEnded):
                        self._answer(conn, event.stream_id, streams.pop(event.stream_id))
                writer.write(conn.data_to_send())
                await writer.drain()
        except Exception as exc:  # the test reads it: a client that does not speak HTTP/2 lands here
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            writer.close()

    def _answer(self, conn, stream_id: int, request: dict) -> None:
        headers = request["headers"]
        token = headers[":path"].rsplit("/", 1)[-1]
        apns_id = str(uuid.uuid4()).upper()
        self.pushes.append(ApnsPush(headers, request["body"], apns_id))
        status, reason = self.answers.get(token, (200, ""))
        body = json.dumps({"reason": reason}).encode() if reason else b""
        answer = [(":status", str(status)), ("apns-id", apns_id)]
        if body:
            answer += [("content-type", "application/json"), ("content-length", str(len(body)))]
        conn.send_headers(stream_id, answer, end_stream=not body)
        if body:
            conn.send_data(stream_id, body, end_stream=True)


@pytest.fixture(scope="module")
def apple():
    servers = {environment: FakeApns() for environment in ("sandbox", "production")}
    yield servers
    for server in servers.values():
        server.close()


@pytest.fixture
def apns_service(apple, monkeypatch) -> dict[str, FakeApns]:
    for server in apple.values():
        server.reset()
    monkeypatch.setattr(push_native.ApnsChannel, "origins", {name: server.origin for name, server in apple.items()})
    push_native.ApnsChannel.tokens.clear()
    yield apple
    push_native.ApnsChannel.tokens.clear()
    for server in apple.values():
        assert server.errors == []


# The fake Google: its token endpoint and FCM's HTTP v1 API


def fcm_error(status_code: int, status: str, code: str | None = None) -> dict:
    """An error of FCM's HTTP v1 API, with its FcmError code when ``code`` is given."""
    details = [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": code}] if code else []
    return {"error": {"code": status_code, "message": "fake", "status": status, "details": details}}


class FakeGoogle:
    """Google's token endpoint, which checks the service account's assertion and gives an access token, and FCM's
    send method, which records each message and answers 200 with its name, or (status, body, headers) as ``answers``
    says for a registration token."""

    def __init__(self):
        self.assertions: list[dict] = []
        self.issued: list[str] = []
        self.token_answer: tuple[int, dict] | None = None
        self.messages: list[tuple[str, str, dict]] = []  # (URL, Authorization, body)
        self.answers: dict[str, tuple[int, dict, dict]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == push_native.GOOGLE_TOKEN_URL:
            form = dict(parse_qsl(request.content.decode()))
            header, claims = verify_jwt(form["assertion"], FCM_KEY.public_key())
            assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
            assert header == {"alg": "RS256", "typ": "JWT"}
            self.assertions.append(claims)
            if self.token_answer is not None:
                return httpx.Response(self.token_answer[0], json=self.token_answer[1])
            self.issued.append(f"ya29.fake-access-token-{len(self.issued)}-{uuid.uuid4().hex}")
            return httpx.Response(
                200, json={"access_token": self.issued[-1], "expires_in": 3599, "token_type": "Bearer"}
            )
        body = json.loads(request.content)
        self.messages.append((str(request.url), request.headers.get("authorization", ""), body))
        name = f"projects/{FCM_PROJECT}/messages/0:{uuid.uuid4().hex}"
        status, answer, headers = self.answers.get(body["message"]["token"], (200, {"name": name}, {}))
        return httpx.Response(status, json=answer, headers=headers)


@pytest.fixture
def google(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr(push_native.FcmChannel, "transport", httpx.MockTransport(fake))
    push_native.FcmChannel.tokens.clear()
    yield fake
    push_native.FcmChannel.tokens.clear()


@pytest.fixture
def client(hub_db, tmp_path, github):
    with make_client(hub_db, tmp_path, github, **APNS_CONFIG, **FCM_CONFIG) as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    headers = members(client, github)
    push(client, headers["owner"], plan_body())
    return headers


def iphone(token: str, environment: str = "sandbox") -> dict:
    return {**apns(token), "environment": environment}


def android(name: str) -> dict:
    return fcm(f"fcm-token-{name}-" + "q" * 40)


def device_rows(db) -> dict[int, tuple]:
    """(state, attempts, last_error) of each delivery to a device, by device."""
    query = select(DELIVERIES.c.device_id, DELIVERIES.c.state, DELIVERIES.c.attempts, DELIVERIES.c.last_error)
    return {row[0]: tuple(row[1:]) for row in sql(db, query.where(DELIVERIES.c.device_id.is_not(None)))}


def due_now(db) -> None:
    """Every delivery to a device that waits, due now."""
    sql(db, update(DELIVERIES).values(next_at=func.now()).where(DELIVERIES.c.device_id.is_not(None)))


def removed_by_hub(db) -> list[str]:
    return sorted(target for action, target in audited(db) if action == "push_device.remove" and "by=hub" in target)


# Which senders are on


def test_push_config_says_apns_and_fcm_are_on_with_their_variables_and_off_without(hub_db, tmp_path, github):
    with make_client(hub_db, tmp_path, github, **APNS_CONFIG, **FCM_CONFIG) as both:
        assert both.get("/v1/push/config").json()["senders"] == {"webpush": False, "apns": True, "fcm": True}
    partial = {**APNS_CONFIG, "apns_key_id": None}
    with make_client(hub_db, tmp_path, github, **partial) as only_apns_key:
        assert only_apns_key.get("/v1/push/config").json()["senders"] == {"webpush": False, "apns": False, "fcm": False}
    with make_client(hub_db, tmp_path, github, **FCM_CONFIG) as only_fcm:
        assert only_fcm.get("/v1/push/config").json()["senders"] == {"webpush": False, "apns": False, "fcm": True}


def test_a_sender_without_its_variables_fails_its_deliveries_at_once_and_its_test_push_is_503(
    hub_db, tmp_path, github, google
):
    with make_client(hub_db, tmp_path, github, **{**APNS_CONFIG, "apns_team_id": None}, **FCM_CONFIG) as client:
        hub = members(client, github)
        push(client, hub["owner"], plan_body())
        phone = added(client, hub["owner"], iphone("ab" * 32))
        added(client, hub["owner"], android("pixel"))
        asked_decision(client, hub)
        assert deliver(client) == {"delivered": 2, "retried": 0, "failed": 1}
        assert device_rows(hub_db)[phone["id"]] == (
            "failed",
            1,
            "Undeliverable: APNs is off on this hub: set EVO_HUB_APNS_TEAM_ID",
        )
        test = client.post(f"/v1/me/push/devices/{phone['id']}/test", headers=hub["owner"])
        assert test.status_code == 503 and "EVO_HUB_APNS_TEAM_ID" in test.json()["message"]
        assert len(google.messages) == 1 and phone["id"] in {d["id"] for d in devices(client, hub["owner"])}


# APNs


def test_a_decision_reaches_each_iphone_over_http2_signed_with_one_provider_token(client, hub, hub_db, apns_service):
    unrestrict(client, hub)
    sandbox, production = "ab" * 32, "cd" * 40
    added(client, hub["owner"], iphone(sandbox))
    added(client, hub["owner"], iphone(production, "production"))
    _, decision = asked_decision(client, hub)
    assert deliver(client) == {"delivered": 3, "retried": 0, "failed": 0}
    (tested,) = apns_service["sandbox"].pushes
    (live,) = apns_service["production"].pushes
    assert (tested.path, live.path) == (f"/3/device/{sandbox}", f"/3/device/{production}")
    for seen in (tested, live):
        headers = seen.headers
        assert (headers[":method"], headers["apns-topic"]) == ("POST", "org.example.hub")
        assert (headers["apns-push-type"], headers["apns-priority"]) == ("alert", "10")
        assert time.time() + 24 * 3600 - 120 < int(headers["apns-expiration"]) <= time.time() + 24 * 3600
        scheme, _, token = headers["authorization"].partition(" ")
        header, claims = verify_jwt(token, APNS_KEY.public_key())
        assert (scheme, header, claims["iss"]) == ("bearer", {"alg": "ES256", "kid": KEY_ID}, TEAM_ID)
        assert abs(claims["iat"] - time.time()) < 120 and set(claims) == {"iss", "iat"}
        assert seen.shown() == {
            "aps": {
                "alert": {"title": TITLE_OF.format(run=decision["run_id"]), "body": BODY},
                "sound": "default",
                "thread-id": PROJECT,
            },
            "url": f"{HUB_URL}/inbox?decision={decision['id']}",
            "tag": headers["apns-collapse-id"],
            "kind": "decision",
            "project": PROJECT,
        }
    assert tested.headers["authorization"] == live.headers["authorization"], "one provider token, signed once"
    token = tested.headers["authorization"].split(" ", 1)[1]
    assert token not in scrub(f"signed in with {token}"), "a kept provider token is a secret"
    external = sql(hub_db, select(DELIVERIES.c.external_id).where(DELIVERIES.c.device_id.is_not(None)))
    assert sorted(row[0] for row in external) == sorted([tested.apns_id, live.apns_id])
    assert all(d["last_sent_at"] is not None for d in devices(client, hub["owner"]))


def test_apns_410_bad_device_token_and_unregistered_remove_only_their_device(client, hub, hub_db, apns_service):
    answers = {
        "a1" * 32: (410, "Unregistered"),
        "a2" * 32: (400, "BadDeviceToken"),
        "a3" * 32: (410, "ExpiredToken"),
        "a4" * 32: (400, "DeviceTokenNotForTopic"),
        "a5" * 32: (429, "TooManyRequests"),
        "a6" * 32: (503, "ServiceUnavailable"),
        "a7" * 32: (200, ""),
    }
    apns_service["sandbox"].answers.update(answers)
    ids = {token: added(client, hub["owner"], iphone(token))["id"] for token in answers}
    asked_decision(client, hub)
    assert deliver(client) == {"delivered": 2, "retried": 2, "failed": 4}
    kept = {"a4" * 32, "a5" * 32, "a6" * 32, "a7" * 32}
    assert {d["id"] for d in devices(client, hub["owner"])} == {ids[token] for token in kept}
    rows = device_rows(hub_db)
    assert set(rows) == {ids[token] for token in kept}, "a removed device's deliveries went with it"
    assert rows[ids["a4" * 32]] == ("failed", 1, "Undeliverable: APNs answered 400: DeviceTokenNotForTopic")
    assert rows[ids["a5" * 32]][:2] == ("pending", 0), "a 429 waits without counting a try"
    assert rows[ids["a6" * 32]][:2] == ("pending", 1) and "503" in rows[ids["a6" * 32]][2]
    assert rows[ids["a7" * 32]][:2] == ("delivered", 1)
    gone = ("a1" * 32, "a2" * 32, "a3" * 32)
    assert removed_by_hub(hub_db) == sorted(f"push_device:{ids[t]} kind=apns user={OWNER} by=hub" for t in gone)
    assert not any(token in target for _, target in audited(hub_db) for token in answers)


def test_an_expired_provider_token_is_dropped_and_the_next_try_signs_a_new_one(client, hub, hub_db, apns_service):
    phone = "be" * 32
    added(client, hub["owner"], iphone(phone))
    apns_service["sandbox"].answers[phone] = (403, "ExpiredProviderToken")
    asked_decision(client, hub)
    assert deliver(client) == {"delivered": 1, "retried": 1, "failed": 0}
    apns_service["sandbox"].answers.clear()
    due_now(hub_db)
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 0}
    first, second = apns_service["sandbox"].pushes
    assert first.headers["authorization"] != second.headers["authorization"]
    verify_jwt(second.headers["authorization"].split(" ", 1)[1], APNS_KEY.public_key())


# FCM


def test_a_decision_reaches_each_android_phone_with_one_access_token_of_the_service_account(
    client, hub, hub_db, google
):
    unrestrict(client, hub)
    phones = [android("pixel"), android("tablet")]
    for body in phones:
        added(client, hub["owner"], body)
    _, decision = asked_decision(client, hub)
    assert deliver(client) == {"delivered": 3, "retried": 0, "failed": 0}
    (claims,) = google.assertions
    assert claims == {
        "iss": FCM_EMAIL,
        "scope": "https://www.googleapis.com/auth/firebase.messaging",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": claims["iat"],
        "exp": claims["iat"] + 3600,
    }
    assert abs(claims["iat"] - time.time()) < 120
    (access,) = google.issued
    url = f"https://fcm.googleapis.com/v1/projects/{FCM_PROJECT}/messages:send"
    assert [(seen_url, auth) for seen_url, auth, _ in google.messages] == [(url, f"Bearer {access}")] * 2
    assert sorted(body["message"]["token"] for _, _, body in google.messages) == sorted(p["token"] for p in phones)
    message = google.messages[0][2]["message"]
    tag = message["data"]["tag"]
    assert message == {
        "token": message["token"],
        "notification": {"title": TITLE_OF.format(run=decision["run_id"]), "body": BODY},
        "data": {
            "url": f"{HUB_URL}/inbox?decision={decision['id']}",
            "tag": tag,
            "kind": "decision",
            "project": PROJECT,
        },
        "android": {"priority": "HIGH", "ttl": "86400s", "notification": {"tag": tag}},
    }
    assert access not in scrub(f"Authorization: Bearer {access}"), "a kept access token is a secret"
    external = sql(hub_db, select(DELIVERIES.c.external_id).where(DELIVERIES.c.device_id.is_not(None)))
    assert all(row[0].startswith(f"projects/{FCM_PROJECT}/messages/") for row in external)


def test_fcm_unregistered_removes_only_that_device_and_other_answers_wait_retry_or_fail(client, hub, hub_db, google):
    bodies = {name: android(name) for name in ("gone", "invalid", "busy", "broken", "fine")}
    tokens = {name: body["token"] for name, body in bodies.items()}
    google.answers[tokens["gone"]] = (404, fcm_error(404, "NOT_FOUND", "UNREGISTERED"), {})
    google.answers[tokens["invalid"]] = (400, fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT"), {})
    google.answers[tokens["busy"]] = (
        429,
        fcm_error(429, "RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"),
        {"Retry-After": "90"},
    )
    google.answers[tokens["broken"]] = (500, fcm_error(500, "INTERNAL", "INTERNAL"), {})
    ids = {name: added(client, hub["owner"], body)["id"] for name, body in bodies.items()}
    asked_decision(client, hub)
    assert deliver(client) == {"delivered": 2, "retried": 2, "failed": 2}
    assert {d["id"] for d in devices(client, hub["owner"])} == {ids[n] for n in ("invalid", "busy", "broken", "fine")}
    rows = device_rows(hub_db)
    assert ids["gone"] not in rows
    assert rows[ids["invalid"]] == ("failed", 1, "Undeliverable: FCM answered 400: INVALID_ARGUMENT")
    assert rows[ids["busy"]][:2] == ("pending", 0)
    wait = sql(
        hub_db,
        select(extract("epoch", DELIVERIES.c.next_at - func.now())).where(DELIVERIES.c.device_id == ids["busy"]),
    )[0][0]
    assert 70 < wait <= 90
    assert rows[ids["broken"]][:2] == ("pending", 1) and "500" in rows[ids["broken"]][2]
    assert rows[ids["fine"]][:2] == ("delivered", 1)
    assert removed_by_hub(hub_db) == [f"push_device:{ids['gone']} kind=fcm user={OWNER} by=hub"]
    assert len(google.assertions) == 1


def test_fcm_401_drops_the_access_token_and_the_next_try_takes_a_new_one(client, hub, hub_db, google):
    phone = android("pixel")
    added(client, hub["owner"], phone)
    google.answers[phone["token"]] = (401, fcm_error(401, "UNAUTHENTICATED"), {})
    asked_decision(client, hub)
    assert deliver(client) == {"delivered": 1, "retried": 1, "failed": 0}
    google.answers.clear()
    due_now(hub_db)
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 0}
    assert len(google.issued) == 2
    assert [auth for _, auth, _ in google.messages] == [f"Bearer {token}" for token in google.issued]


def test_a_service_account_google_refuses_fails_the_delivery_and_keeps_the_device(client, hub, hub_db, google):
    phone = added(client, hub["owner"], android("pixel"))
    google.token_answer = (400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
    asked_decision(client, hub)
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 1}
    state, attempts, error = device_rows(hub_db)[phone["id"]]
    assert (state, attempts) == ("failed", 1)
    assert error == (
        "Undeliverable: Google's token endpoint answered 400: invalid_grant Invalid JWT Signature.; "
        "check EVO_HUB_FCM_SERVICE_ACCOUNT"
    )
    assert google.messages == [] and [d["id"] for d in devices(client, hub["owner"])] == [phone["id"]]


# What both carry, and a test push


def test_redact_a_restricted_project_push_to_an_app_carries_only_the_kind_the_project_and_the_link(
    client, hub, apns_service, google
):
    added(client, hub["owner"], iphone("ab" * 32))
    added(client, hub["owner"], android("pixel"))
    restrict(client, hub)
    _, decision = asked_decision(client, hub, context=f"## Why\n{CONTEXT_SECRET}")
    deliver(client)
    (seen,) = apns_service["sandbox"].pushes
    shown = seen.shown()
    assert shown["aps"]["alert"] == {"title": "Decision needed", "body": PROJECT}
    assert shown["url"] == f"{HUB_URL}/inbox?decision={decision['id']}"
    (_, _, body) = google.messages[0]
    assert body["message"]["notification"] == {"title": "Decision needed", "body": PROJECT}
    for carried in (json.dumps(shown), json.dumps(body)):
        assert QUESTION not in carried and CONTEXT_SECRET not in carried


def test_a_test_push_reaches_an_iphone_and_one_fcm_no_longer_knows_is_removed(client, hub, apns_service, google):
    phone = added(client, hub["owner"], iphone("ab" * 32, "production"))
    tablet = added(client, hub["owner"], android("tablet"))
    response = client.post(f"/v1/me/push/devices/{phone['id']}/test", headers=hub["owner"])
    assert response.status_code == 200, response.text
    assert response.json() == {"sent": True, "removed": False, "error": None}
    (seen,) = apns_service["production"].pushes
    assert seen.shown()["aps"]["alert"] == {"title": "Test notification", "body": push_payload.TEST_BODY}
    assert seen.headers["apns-collapse-id"] == "evo-test"
    google.answers[android("tablet")["token"]] = (404, fcm_error(404, "NOT_FOUND", "UNREGISTERED"), {})
    gone = client.post(f"/v1/me/push/devices/{tablet['id']}/test", headers=hub["owner"]).json()
    assert (gone["sent"], gone["removed"]) == (False, True) and "UNREGISTERED" in gone["error"]
    assert [d["id"] for d in devices(client, hub["owner"])] == [phone["id"]]


def test_payload_to_apns_and_fcm_fits_four_kilobytes_whatever_the_title_and_the_question():
    from types import SimpleNamespace

    huge = SimpleNamespace(
        id=7,
        kind="decision",
        notice_kind=None,
        project="p" * 100,
        decision_id=3,
        title="é" * 5000,
        body=None,
        link="/inbox?decision=3",
        extra={
            "restricted": False,
            "hide_details": False,
            "decision": {"question": "\U0001f600" * 9000, "options": []},
        },
    )
    shown = push_payload.payload(huge, HUB_URL)
    assert len(push_native.apns_body(shown)) <= push_native.APNS_MAX_BYTES
    message = push_native.fcm_message(shown, "fcm-token-" + "x" * 140)["message"]
    carried = {**message["notification"], **message["data"]}
    assert len(push_payload.encoded(carried)) <= 4096
