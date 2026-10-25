"""The senders of the hub's apps: APNs to iPhones and iPads, FCM to Android phones. ``notifications.CHANNELS`` names
them for the kinds apns and fcm of push_devices, as it names WebPushChannel for webpush (``push_senders``), so a
delivery to such a device goes through the outbox with the same grant check, backoff and RetryAfter, and carries what
a Web Push carries (``push_payload``, whose redaction is the same), in the form of its service.

ApnsChannel posts to the APNs provider API over HTTP/2, the only protocol it takes: POST /3/device/<token> on
api.push.apple.com, or on api.sandbox.push.apple.com for a device whose environment is sandbox, with the device's topic,
push type alert, priority 10, an expiration TTL_SECONDS ahead and the notification's tag as its collapse id. It signs
in with a provider token, a JWT signed with ES256 by the hub's APNs key (EVO_HUB_APNS_*) naming the key and the team,
made once each PROVIDER_TOKEN_SECONDS: APNs refuses a token older than an hour and a new one more often than every
20 minutes. APNs answering 410, BadDeviceToken or Unregistered says the device's token is gone, which removes that
device alone; 429 waits; ExpiredProviderToken drops the token, to try again later with a new one; another 4xx fails
the delivery and keeps the device; anything else is tried again with the outbox's backoff.

FcmChannel posts to the FCM HTTP v1 API, POST /v1/projects/<project>/messages:send, with an OAuth access token of the
hub's service account (EVO_HUB_FCM_SERVICE_ACCOUNT): a JWT signed with RS256 by the account's key, exchanged at Google's
token endpoint for the firebase.messaging scope and kept until TOKEN_MARGIN_SECONDS before it expires. FCM's error
code UNREGISTERED says the device's token is gone, which removes that device alone; 401 drops the access token, to try
again later with a new one; 429 waits; another 4xx fails the delivery and keeps the device; anything else is tried
again.

Each class keeps its tokens for the process (``Tokens``), each registered as a secret while it is kept, so no log line
carries one. Tests hand in the hosts (``ApnsChannel.origins``) or the HTTP transport (``FcmChannel.transport``): no
test calls Apple or Google.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import ClassVar

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from evo_agents.hub import push_payload
from evo_agents.hub.config import APNS_VARIABLES, FCM_VARIABLE
from evo_agents.hub.log import register_secret, unregister_secret
from evo_agents.hub.server.notifications import ChannelGone, RetryAfter, Undeliverable
from evo_agents.hub.server.push import APNS_TOKEN, APNS_TOPIC, FCM_TOKEN
from evo_agents.hub.server.push_senders import (
    HTTP_TIMEOUT_SECONDS,
    MAX_REASON_CHARS,
    TTL_SECONDS,
    PushSender,
    retry_after,
)
from evo_agents.hub.server.webpush import b64url
from evo_agents.hub.tables_push import APNS, APNS_ENVIRONMENTS, FCM

PROVIDER_TOKEN_SECONDS = 50 * 60  # how long the hub signs in to APNs with one provider token: 20 to 60 minutes
APNS_ORIGINS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
APNS_GONE = ("BadDeviceToken", "Unregistered")  # the reasons that say a device's token no longer reaches it
APNS_MAX_BYTES = 4096  # the most an alert's JSON may take
APNS_COLLAPSE_BYTES = 64  # of apns-collapse-id
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_URL = "https://fcm.googleapis.com"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"  # the grant of a signed assertion (RFC 7523)
ASSERTION_SECONDS = 3600  # the most Google takes
TOKEN_MARGIN_SECONDS = 300  # an access token is dropped this long before it expires
FCM_GONE = "UNREGISTERED"  # the error code that says a device's token no longer reaches it


class Tokens:
    """The bearer tokens of a sender, one per key it signs with, each kept until its time is up and registered as a
    secret while it is kept."""

    def __init__(self):
        self._kept: dict[str, tuple[float, str]] = {}

    def get(self, key: str, now: float) -> str | None:
        found = self._kept.get(key)
        return found[1] if found is not None and now < found[0] else None

    def keep(self, key: str, token: str, until: float) -> None:
        self.forget(key)
        register_secret(token)
        self._kept[key] = (until, token)

    def forget(self, key: str) -> None:
        dropped = self._kept.pop(key, None)
        if dropped is not None:
            unregister_secret(dropped[1])

    def clear(self) -> None:
        for key in list(self._kept):
            self.forget(key)


def _key_of(*parts: str) -> str:
    """Which key a token is of: a digest of what signs it, so a new key never reuses an old key's token."""
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def _jwt_part(value: dict) -> str:
    return b64url(json.dumps(value, separators=(",", ":")).encode())


def _private_key(pem: str, variable: str):
    try:
        return serialization.load_pem_private_key(pem.encode(), password=None)
    except (TypeError, ValueError):
        raise Undeliverable(f"the key in {variable} does not load") from None


def provider_token(private_key: str, key_id: str, team_id: str, now: float) -> str:
    """An APNs provider token: a JWT naming the key ``key_id`` and the team ``team_id``, issued at ``now``, signed with
    ES256 by ``private_key`` (the .p8 of that key)."""
    key = _private_key(private_key, APNS_VARIABLES[2])
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise Undeliverable(f"the key in {APNS_VARIABLES[2]} is not a P-256 key")
    signed = f"{_jwt_part({'alg': 'ES256', 'kid': key_id})}.{_jwt_part({'iss': team_id, 'iat': int(now)})}"
    r, s = decode_dss_signature(key.sign(signed.encode(), ec.ECDSA(hashes.SHA256())))
    return f"{signed}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def google_assertion(client_email: str, private_key: str, now: float) -> str:
    """The JWT a service account trades for an access token of FCM_SCOPE at GOOGLE_TOKEN_URL (RFC 7523), signed with
    RS256 by its key."""
    key = _private_key(private_key, FCM_VARIABLE)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise Undeliverable(f"the private key in {FCM_VARIABLE} is not an RSA key")
    issued = int(now)
    claims = {
        "iss": client_email,
        "scope": FCM_SCOPE,
        "aud": GOOGLE_TOKEN_URL,
        "iat": issued,
        "exp": issued + ASSERTION_SECONDS,
    }
    signed = f"{_jwt_part({'alg': 'RS256', 'typ': 'JWT'})}.{_jwt_part(claims)}"
    return f"{signed}.{b64url(key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256()))}"


def _json(answer: httpx.Response) -> dict:
    try:
        found = answer.json()
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _clip(text) -> str:
    return " ".join(str(text).split())[:MAX_REASON_CHARS] if text else ""


def apns_body(payload: dict) -> bytes:
    """The JSON APNs takes for ``payload`` (``push_payload``): its title and body as the alert, the rest beside it."""
    aps: dict = {"alert": {key: payload[key] for key in ("title", "body") if payload.get(key)}, "sound": "default"}
    if payload.get("project"):
        aps["thread-id"] = payload["project"]
    custom = {key: payload[key] for key in ("url", "tag", "kind", "project") if payload.get(key) is not None}
    return push_payload.encoded({"aps": aps, **custom})


class ApnsChannel(PushSender):
    """APNs to an app's device token, over HTTP/2 (the module)."""

    kind = APNS
    origins: ClassVar[dict[str, str]] = APNS_ORIGINS  # tests point both environments at a fake APNs
    tokens: ClassVar[Tokens] = Tokens()

    @classmethod
    def missing(cls, config) -> list[str]:
        return list(APNS_VARIABLES) if config is None else config.apns_missing()

    def _key(self) -> str:
        return _key_of(self.config.apns_team_id, self.config.apns_key_id, self.config.apns_private_key)

    def _token(self, key: str, now: float) -> str:
        token = self.tokens.get(key, now)
        if token is None:
            config = self.config
            token = provider_token(config.apns_private_key, config.apns_key_id, config.apns_team_id, now)
            self.tokens.keep(key, token, now + PROVIDER_TOKEN_SECONDS)
        return token

    async def deliver(self, payload: dict, config: dict) -> str | None:
        missing = self.missing(self.config)
        if missing:
            raise Undeliverable(f"APNs is off on this hub: set {', '.join(missing)}")
        token, topic, environment = config.get("address"), config.get("topic"), config.get("environment")
        if not (
            isinstance(token, str)
            and re.fullmatch(APNS_TOKEN, token)
            and isinstance(topic, str)
            and re.fullmatch(APNS_TOPIC, topic)
            and environment in APNS_ENVIRONMENTS
        ):
            raise Undeliverable("the device's token, topic or environment is not one APNs takes")
        body = apns_body(payload)
        if len(body) > APNS_MAX_BYTES:
            raise Undeliverable(f"the push takes {len(body)} bytes, over the {APNS_MAX_BYTES} APNs takes")
        now, key = time.time(), self._key()
        headers = {
            "authorization": f"bearer {self._token(key, now)}",
            "apns-topic": topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": str(int(now) + TTL_SECONDS),
            "apns-collapse-id": payload.get("tag", "").encode()[:APNS_COLLAPSE_BYTES].decode(errors="ignore"),
        }
        # http1=False: HTTP/2 alone, through ALPN over TLS (and with prior knowledge over the plain http of a test)
        async with httpx.AsyncClient(http1=False, http2=True, timeout=HTTP_TIMEOUT_SECONDS) as http:
            answer = await http.post(
                f"{self.origins[environment]}/3/device/{token.lower()}", content=body, headers=headers
            )
        return self._outcome(answer, key)

    def _outcome(self, answer: httpx.Response, key: str) -> str | None:
        """The apns-id of a push APNs took, or the exception its refusal is."""
        status = answer.status_code
        if 200 <= status < 300:
            return answer.headers.get("apns-id")
        reason = _json(answer).get("reason")
        reason = reason if isinstance(reason, str) else ""
        said = f"APNs answered {status}" + (f": {_clip(reason)}" if reason else "")
        if status == 410 or reason in APNS_GONE:
            raise ChannelGone(f"{said}; the device's token no longer reaches it")
        if status == 429:
            raise RetryAfter(retry_after(answer), said)
        if reason == "ExpiredProviderToken":
            self.tokens.forget(key)
            raise RuntimeError(said)
        if 400 <= status < 500:
            raise Undeliverable(said)
        raise RuntimeError(said)


def fcm_message(payload: dict, token: str) -> dict:
    """The message FCM's HTTP v1 API takes for ``payload`` (``push_payload``) to the device ``token``: its title and
    body as the notification, the rest as data (strings), high priority, kept TTL_SECONDS, replacing a notification
    of the same tag."""
    notification = {key: payload[key] for key in ("title", "body") if payload.get(key)}
    data = {key: str(payload[key]) for key in ("url", "tag", "kind", "project") if payload.get(key) is not None}
    android = {"priority": "HIGH", "ttl": f"{TTL_SECONDS}s", "notification": {"tag": payload.get("tag", "")}}
    return {"message": {"token": token, "notification": notification, "data": data, "android": android}}


class FcmChannel(PushSender):
    """Firebase Cloud Messaging to an app's registration token, through the HTTP v1 API (the module)."""

    kind = FCM
    transport: ClassVar[httpx.AsyncBaseTransport | None] = None  # tests hand Google's fake endpoints in here
    tokens: ClassVar[Tokens] = Tokens()

    @classmethod
    def missing(cls, config) -> list[str]:
        return [FCM_VARIABLE] if config is None else config.fcm_missing()

    def _key(self) -> str:
        return _key_of(self.config.fcm_client_email, self.config.fcm_private_key)

    async def _access_token(self, http: httpx.AsyncClient, key: str, now: float) -> str:
        """The service account's access token, from Google's token endpoint when none is kept."""
        token = self.tokens.get(key, now)
        if token is not None:
            return token
        assertion = google_assertion(self.config.fcm_client_email, self.config.fcm_private_key, now)
        answer = await http.post(GOOGLE_TOKEN_URL, data={"grant_type": JWT_BEARER, "assertion": assertion})
        found = _json(answer)
        if answer.status_code != 200:
            error = _clip(" ".join(str(found.get(name) or "") for name in ("error", "error_description")))
            said = f"Google's token endpoint answered {answer.status_code}" + (f": {error}" if error else "")
            if 400 <= answer.status_code < 500 and answer.status_code != 429:
                raise Undeliverable(f"{said}; check {FCM_VARIABLE}")
            raise RuntimeError(said)
        token, lasts = found.get("access_token"), found.get("expires_in", ASSERTION_SECONDS)
        if not isinstance(token, str) or not token or not isinstance(lasts, int | float):
            raise RuntimeError("Google's token endpoint answered without an access token")
        self.tokens.keep(key, token, now + max(0.0, float(lasts) - TOKEN_MARGIN_SECONDS))
        return token

    async def deliver(self, payload: dict, config: dict) -> str | None:
        missing = self.missing(self.config)
        if missing:
            raise Undeliverable(f"FCM is off on this hub: set {', '.join(missing)}")
        token = config.get("address")
        if not isinstance(token, str) or not re.fullmatch(FCM_TOKEN, token):
            raise Undeliverable("the device's token is not one FCM takes")
        now, key = time.time(), self._key()
        url = f"{FCM_URL}/v1/projects/{self.config.fcm_project_id}/messages:send"
        async with httpx.AsyncClient(transport=self.transport, timeout=HTTP_TIMEOUT_SECONDS) as http:
            access = await self._access_token(http, key, now)
            answer = await http.post(
                url,
                content=push_payload.encoded(fcm_message(payload, token)),
                headers={"authorization": f"Bearer {access}", "content-type": "application/json; charset=UTF-8"},
            )
        return self._outcome(answer, key)

    def _outcome(self, answer: httpx.Response, key: str) -> str | None:
        """The name FCM gave a message it took, or the exception its refusal is."""
        status = answer.status_code
        found = _json(answer)
        if 200 <= status < 300:
            name = found.get("name")
            return name if isinstance(name, str) else None
        error = found.get("error") if isinstance(found.get("error"), dict) else {}
        details = error.get("details") if isinstance(error.get("details"), list) else []
        codes = [item.get("errorCode") for item in details if isinstance(item, dict)]
        code = next((code for code in codes if isinstance(code, str)), "") or error.get("status") or ""
        said = f"FCM answered {status}" + (f": {_clip(code)}" if code else "")
        if FCM_GONE in codes:
            raise ChannelGone(f"{said}; the device's token no longer reaches it")
        if status == 401:
            self.tokens.forget(key)
            raise RuntimeError(said)
        if status == 429:
            raise RetryAfter(retry_after(answer), said)
        if 400 <= status < 500:
            raise Undeliverable(said)
        raise RuntimeError(said)
