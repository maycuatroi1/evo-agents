"""Web Push as the hub sends it: a message encrypted for one browser subscription (RFC 8291, the ``aes128gcm``
content coding of RFC 8188) and the VAPID header that tells the push service which server sends it (RFC 8292).

The hub's VAPID key pair is one P-256 key: EVO_HUB_VAPID_PRIVATE_KEY holds its private scalar, 32 bytes in base64url
(``evo_agents.hub.config``), and the public key a browser subscribes with is derived from it (``public_key``), so the
two can never disagree. ``vapid_authorization`` signs a JWT (ES256) for the origin of a subscription's endpoint, with
the subject the push service may write to; ``encrypt`` makes the body a push service takes, a single record of at most
RECORD_SIZE bytes, with a key pair of its own for each message.

Which hosts may receive a message is ``endpoint_problem``'s to say: an https URL on a host of a known push service
(PUSH_HOSTS), so a member cannot make the hub post to an address of its own network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_SIZE = 4096  # the record size the hub writes, and the most a push service must take (RFC 8291, section 4)
KEY_BYTES = 65  # an uncompressed P-256 point: 0x04, then x and y
AUTH_BYTES = 16  # the subscription's authentication secret
SALT_BYTES = 16
TAG_BYTES = 16  # AES-GCM's
HEADER_BYTES = SALT_BYTES + 4 + 1 + KEY_BYTES  # salt, record size, key id length, key id (the sender's public key)
MAX_PLAINTEXT = RECORD_SIZE - HEADER_BYTES - TAG_BYTES - 1  # 3993: one record, its padding delimiter included
JWT_SECONDS = 12 * 3600  # how long a VAPID JWT lives; RFC 8292 caps it at 24 hours
# The hosts of the push services browsers subscribe with: Chrome, Edge and other Chromium browsers (FCM), Firefox
# (Mozilla autopush), Safari (Apple), and Edge on Windows (WNS). A host is one of them or under one of them.
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)
MAX_ENDPOINT_CHARS = 2048


def b64url(data: bytes) -> str:
    """``data`` in base64url without padding, as Web Push writes keys and JWTs."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    """The bytes of base64url ``text``, padded or not; ValueError for anything else."""
    if not isinstance(text, str) or not text or any(ch not in _B64URL for ch in text.rstrip("=")):
        raise ValueError("not base64url")
    stripped = text.rstrip("=")
    return base64.urlsafe_b64decode(stripped + "=" * (-len(stripped) % 4))


_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def endpoint_problem(endpoint: str) -> str | None:
    """Why the hub would not post to ``endpoint``, or None: an https URL with no credential or fragment, on the
    default port of a host of PUSH_HOSTS, at most MAX_ENDPOINT_CHARS characters."""
    if not isinstance(endpoint, str) or not 0 < len(endpoint) <= MAX_ENDPOINT_CHARS:
        return f"a subscription's endpoint is an https URL of at most {MAX_ENDPOINT_CHARS} characters"
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return "a subscription's endpoint is an https URL"
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password or parts.fragment:
        return "a subscription's endpoint is an https URL without a user, a password or a fragment"
    if port not in (None, 443) or not any(host == known or host.endswith("." + known) for known in PUSH_HOSTS):
        return f"the hub sends Web Push only to the push services of browsers: {', '.join(PUSH_HOSTS)}"
    return None


def subscriber_key(p256dh: str) -> bytes:
    """The browser's public key of a subscription (``keys.p256dh``) as its 65 bytes; ValueError when it is not a point
    of P-256."""
    raw = unb64url(p256dh)
    if len(raw) != KEY_BYTES or raw[0] != 4:
        raise ValueError("keys.p256dh is an uncompressed P-256 public key, 65 bytes in base64url")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)  # ValueError off the curve
    return raw


def auth_secret(auth: str) -> bytes:
    """The subscription's authentication secret (``keys.auth``) as its 16 bytes; ValueError otherwise."""
    raw = unb64url(auth)
    if len(raw) != AUTH_BYTES:
        raise ValueError("keys.auth is 16 bytes in base64url")
    return raw


@lru_cache(maxsize=4)
def _private_key(private_key: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64url(private_key), "big"), ec.SECP256R1())


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key(private_key: str) -> str:
    """The public VAPID key of the private one (EVO_HUB_VAPID_PRIVATE_KEY), in base64url as a browser's
    ``applicationServerKey`` takes it."""
    return b64url(_point(_private_key(private_key).public_key()))


def audience(endpoint: str) -> str:
    """The origin of ``endpoint``, the ``aud`` of its VAPID JWT."""
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}"


def vapid_authorization(endpoint: str, private_key: str, subject: str, now: float | None = None) -> str:
    """The Authorization header of a push to ``endpoint``: ``vapid t=<JWT>, k=<public key>`` (RFC 8292), the JWT
    signed with ES256 for the endpoint's origin, living JWT_SECONDS, naming ``subject`` (a mailto: or https: URL)."""
    issued = int(time.time() if now is None else now)
    header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = {"aud": audience(endpoint), "exp": issued + JWT_SECONDS, "sub": subject}
    body = b64url(json.dumps(claims, separators=(",", ":")).encode())
    signed = f"{header}.{body}".encode()
    r, s = decode_dss_signature(_private_key(private_key).sign(signed, ec.ECDSA(hashes.SHA256())))
    signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{body}.{signature}, k={public_key(private_key)}"


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt(
    plaintext: bytes,
    p256dh: str,
    auth: str,
    *,
    salt: bytes | None = None,
    sender: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """The body of a push of ``plaintext`` to the subscription whose keys are ``p256dh`` and ``auth``: one record of
    the ``aes128gcm`` content coding (RFC 8188), keyed as RFC 8291 says, with the header naming the sender's public key.
    A new key pair and salt are made for each message unless ``sender`` and ``salt`` are given (the RFC's example).
    ValueError for a key that is not one, or a plaintext over MAX_PLAINTEXT bytes."""
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError(f"a push carries at most {MAX_PLAINTEXT} bytes")
    receiver, secret = subscriber_key(p256dh), auth_secret(auth)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    salt = os.urandom(SALT_BYTES) if salt is None else salt
    sender_key = _point(sender.public_key())
    shared = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver))
    # HKDF with the authentication secret as salt: the input keying material (RFC 8291, section 3.4)
    key_info = b"WebPush: info\x00" + receiver + sender_key
    ikm = _hmac(_hmac(secret, shared), key_info + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    record = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # 0x02: the last record, no padding after it
    return salt + struct.pack("!IB", RECORD_SIZE, KEY_BYTES) + sender_key + record
