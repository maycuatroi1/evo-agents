"""Signatures of plugin bundles, and the publishers a hub trusts.

A bundle is signed by its publisher; the signature travels beside it as a JSON object, the envelope:

- ``{"kind": "sigstore", "sha256": "<hex>", "bundle": {...}}``: the Sigstore bundle that the CI workflow of the
  plugin's repo made with its own OIDC identity, keyless (``sigstore sign`` in GitHub Actions). ``read_signature``
  wraps a Sigstore bundle file as it is, taking ``sha256`` from its message digest.
- ``{"kind": "ed25519", "sha256": "<hex>", "signature": "<base64>"}``: an Ed25519 signature of the bundle's bytes by
  one of the publisher's keys (``sign_ed25519``), for hubs that cannot reach Sigstore.

The operator lists the publishers it trusts in EVO_HUB_PLUGIN_PUBLISHERS, comma-separated entries
``ID=sigstore:OWNER/REPO/.github/workflows/FILE.yml`` (the workflow whose runs may sign) or ``ID=ed25519:KEY`` (the
public key, 32 bytes in base64); one id may have several entries, several workflows or keys being rotated
(``parse_publishers``). A manifest names its publisher by id.

``verify`` checks the envelope against the bundle's bytes and the trusted publishers, in this order, and raises
SignatureError with a ``reason`` and a sentence an admin can act on: ``unsigned`` (no envelope), ``malformed`` (not
an envelope of a known kind), ``altered`` (the bytes are not the ones signed, their SHA-256 differs), ``untrusted``
(the publisher is not listed, or has no identity of the envelope's kind), ``invalid`` (the signature does not verify)
and ``untrusted`` again when a Sigstore signature verifies but its workflow is not one the publisher's entry names.
SignatureUnavailable means the check could not run (the trust root or a package is missing) and says nothing about
the bundle.

Sigstore bundles are checked by ``SigstoreVerifier`` with sigstore-python against Sigstore's public-good trust
root, fetched over TUF and cached; the certificate's issuer must be GitHub Actions' and one of its identities (the
workflow ref, ``https://github.com/OWNER/REPO/.github/workflows/FILE.yml@refs/...``) must be a run of one of the
publisher's workflows, the repository ignoring case, the workflow's path exactly. Ed25519 needs ``cryptography``.
Both are imported only when a signature is checked, so the module loads on a core install.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

SIGNATURE_KINDS = ("sigstore", "ed25519")
REASONS = ("unsigned", "malformed", "altered", "untrusted", "invalid")
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB = "https://github.com"  # where the identity of a workflow run starts
SIGSTORE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle"  # the prefix of every version's mediaType
MAX_SIGNATURE = 64 * 1024  # bytes of an envelope as JSON
PUBLISHERS_VARIABLE = "EVO_HUB_PLUGIN_PUBLISHERS"
PUBLISHER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
SHA256 = re.compile(r"[0-9a-f]{64}")
_ENTRY = re.compile(r"([a-z0-9][a-z0-9-]{0,62})=(sigstore|ed25519):(\S+)")
_WORKFLOW = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml)")
_ISSUER_OIDS = (("1.3.6.1.4.1.57264.1.8", True), ("1.3.6.1.4.1.57264.1.1", False))  # Fulcio's, DER-encoded or raw
ED25519_KEY_BYTES = 32
ED25519_SIGNATURE_BYTES = 64


class SignatureError(ValueError):
    """A bundle whose signature this hub does not accept; ``reason`` is one of REASONS."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class SignatureUnavailable(RuntimeError):
    """The signature could not be checked here: a trust root or a package is missing."""


@dataclass(frozen=True)
class SigstoreIdentity:
    """A GitHub Actions workflow whose runs may sign: ``owner/name`` and ``.github/workflows/<file>``."""

    repository: str
    workflow: str

    def signed(self, identity: str) -> bool:
        """Whether ``identity``, of a Fulcio certificate, is a run of this workflow at any ref:
        ``https://github.com/OWNER/REPO/.github/workflows/FILE.yml@refs/...``, the repository ignoring case as GitHub
        does, the workflow's path exactly."""
        head = f"{GITHUB}/"
        if not identity.startswith(head):
            return False
        rest = identity[len(head) :]
        size = len(self.repository)
        return rest[:size].lower() == self.repository.lower() and rest[size:].startswith(f"/{self.workflow}@")

    def __str__(self) -> str:
        return f"{self.repository}/{self.workflow}"


@dataclass(frozen=True)
class Publisher:
    """A publisher an operator trusts, with the workflows and keys that sign for it."""

    id: str
    sigstore: tuple[SigstoreIdentity, ...] = ()
    ed25519: tuple[bytes, ...] = ()

    def identities(self) -> list[str]:
        return [f"sigstore:{identity}" for identity in self.sigstore] + [key_id(key) for key in self.ed25519]


@dataclass(frozen=True)
class Verified:
    """Who signed a bundle: the publisher, the kind of signature, and the workflow identity or key that signed."""

    publisher: str
    kind: str
    signed_by: str


@dataclass(frozen=True)
class SigstoreClaims:
    """What the certificate of a Sigstore bundle that verified says: its OIDC issuer and its identities."""

    issuer: str | None
    identities: tuple[str, ...]


SigstoreCheck = Callable[[bytes, dict], SigstoreClaims]


def key_id(public_key: bytes) -> str:
    """How messages and the hub name an Ed25519 public key: the start of its SHA-256."""
    return f"ed25519:{hashlib.sha256(public_key).hexdigest()[:16]}"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The publishers an operator trusts


def _public_key(text: str) -> bytes:
    altchars = b"-_" if "-" in text or "_" in text else None  # base64url, as some tools print keys
    try:
        key = base64.b64decode(text + "=" * (-len(text) % 4), altchars=altchars, validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != ED25519_KEY_BYTES:
        raise ValueError(f"{text!r} is not an Ed25519 public key, {ED25519_KEY_BYTES} bytes in base64")
    return key


def parse_publishers(text: str | None) -> tuple[Publisher, ...]:
    """The publishers of an EVO_HUB_PLUGIN_PUBLISHERS value, in the order first named; ValueError naming the entry that
    is wrong."""
    found: dict[str, Publisher] = {}
    for entry in (part.strip() for part in (text or "").split(",")):
        if not entry:
            continue
        match = _ENTRY.fullmatch(entry)
        if match is None:
            raise ValueError(
                f"{PUBLISHERS_VARIABLE} lists ID=sigstore:OWNER/REPO/.github/workflows/FILE.yml or ID=ed25519:KEY "
                f"entries separated by commas, not {entry!r}"
            )
        publisher_id, kind, value = match.groups()
        current = found.get(publisher_id, Publisher(publisher_id))
        if kind == "sigstore":
            workflow = _WORKFLOW.fullmatch(value)
            if workflow is None:
                raise ValueError(f"{entry!r}: a workflow is OWNER/REPO/.github/workflows/FILE.yml, not {value!r}")
            current = replace(current, sigstore=(*current.sigstore, SigstoreIdentity(workflow[1], workflow[2])))
        else:
            current = replace(current, ed25519=(*current.ed25519, _public_key(value)))
        found[publisher_id] = current
    return tuple(found.values())


# Envelopes


def sign_ed25519(data: bytes, private_key: bytes) -> dict:
    """The envelope of an Ed25519 signature of ``data`` by ``private_key``, its 32-byte seed."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(data)
    return {"kind": "ed25519", "sha256": sha256_of(data), "signature": base64.b64encode(signature).decode()}


def sigstore_envelope(bundle: dict) -> dict:
    """The envelope of a Sigstore bundle as ``sigstore sign`` writes it, with the SHA-256 its message digest names."""
    digest = ((bundle.get("messageSignature") or {}).get("messageDigest") or {}) if isinstance(bundle, dict) else {}
    try:
        raw = base64.b64decode(digest.get("digest", ""), validate=True)
    except (binascii.Error, ValueError, TypeError):
        raw = b""
    if digest.get("algorithm") != "SHA2_256" or len(raw) != 32:
        raise SignatureError("malformed", "the Sigstore bundle signs no SHA-256 message digest: sign the bundle's file")
    return {"kind": "sigstore", "sha256": raw.hex(), "bundle": bundle}


def read_signature(text: str | bytes) -> dict:
    """The envelope a signature file holds: an envelope as it is, or a Sigstore bundle wrapped into one."""
    if len(text) > MAX_SIGNATURE:
        raise SignatureError("malformed", f"a signature is at most {MAX_SIGNATURE // 1024} KiB")
    try:
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SignatureError("malformed", f"the signature is not JSON: {exc}") from None
    if isinstance(data, dict) and str(data.get("mediaType", "")).startswith(SIGSTORE_MEDIA_TYPE):
        return sigstore_envelope(data)
    if isinstance(data, dict) and "kind" in data:
        return data
    raise SignatureError("malformed", "the signature is neither a Sigstore bundle nor an envelope with a kind")


def _checked_envelope(signature) -> tuple[str, str]:
    """The kind and SHA-256 of an envelope; SignatureError ``malformed`` when it is not one."""
    if not isinstance(signature, dict):
        raise SignatureError("malformed", "a signature is a JSON object")
    kind, sha256 = signature.get("kind"), signature.get("sha256")
    if kind not in SIGNATURE_KINDS:
        raise SignatureError("malformed", f"a signature's kind is {' or '.join(SIGNATURE_KINDS)}, not {kind!r}")
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
        raise SignatureError("malformed", "a signature names the SHA-256 of the bundle it signs, 64 hex digits")
    payload, expected, article = ("bundle", dict, "a") if kind == "sigstore" else ("signature", str, "an")
    if not isinstance(signature.get(payload), expected):
        raise SignatureError("malformed", f"{article} {kind} signature carries its {payload}")
    return kind, sha256


# Checking


def verify(
    data: bytes,
    signature: dict | None,
    publisher_id: str,
    publishers: Iterable[Publisher],
    sigstore: SigstoreCheck | None = None,
) -> Verified:
    """Who signed bundle ``data`` for publisher ``publisher_id``, the id its manifest names; SignatureError saying why
    the signature is not accepted, SignatureUnavailable when it cannot be checked here."""
    if signature is None:
        raise SignatureError(
            "unsigned",
            f"the bundle is not signed: publisher {publisher_id} signs it in its CI and the signature is sent with it",
        )
    kind, signed = _checked_envelope(signature)
    actual = sha256_of(data)
    if signed != actual:
        raise SignatureError(
            "altered",
            f"the bundle is not the one signed: the signature is of SHA-256 {signed[:16]}..., the bundle's is "
            f"{actual[:16]}...; it was changed after signing",
        )
    publisher = next((p for p in publishers if p.id == publisher_id), None)
    if publisher is None:
        raise SignatureError(
            "untrusted",
            f"publisher {publisher_id} is not trusted on this hub: an operator lists it in {PUBLISHERS_VARIABLE}",
        )
    if kind == "ed25519":
        return _verify_ed25519(data, signature, publisher)
    return _verify_sigstore(data, signature["bundle"], publisher, sigstore or default_sigstore())


def _verify_ed25519(data: bytes, signature: dict, publisher: Publisher) -> Verified:
    if not publisher.ed25519:
        raise SignatureError(
            "untrusted", f"publisher {publisher.id} signs with Sigstore on this hub, not with an Ed25519 key"
        )
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise SignatureUnavailable("Ed25519 signatures need the cryptography package, which is not installed") from None
    try:
        raw = base64.b64decode(signature["signature"], validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != ED25519_SIGNATURE_BYTES:
        raise SignatureError("malformed", f"an Ed25519 signature is {ED25519_SIGNATURE_BYTES} bytes in base64")
    for key in publisher.ed25519:
        try:
            Ed25519PublicKey.from_public_bytes(key).verify(raw, data)
        except InvalidSignature:
            continue
        return Verified(publisher.id, "ed25519", key_id(key))
    keys = ", ".join(key_id(key) for key in publisher.ed25519)
    raise SignatureError(
        "invalid", f"the Ed25519 signature is wrong: no key of publisher {publisher.id} ({keys}) made it"
    )


def _verify_sigstore(data: bytes, bundle: dict, publisher: Publisher, check: SigstoreCheck) -> Verified:
    if not publisher.sigstore:
        raise SignatureError(
            "untrusted", f"publisher {publisher.id} signs with Ed25519 keys on this hub, not with Sigstore"
        )
    claims = check(data, bundle)
    if claims.issuer != GITHUB_ISSUER:
        raise SignatureError(
            "untrusted",
            f"the Sigstore certificate is of an identity of {claims.issuer or 'no issuer'}, not of a GitHub Actions "
            "workflow",
        )
    for identity in publisher.sigstore:
        for signer in claims.identities:
            if identity.signed(signer):
                return Verified(publisher.id, "sigstore", signer)
    workflows = ", ".join(str(identity) for identity in publisher.sigstore)
    raise SignatureError(
        "untrusted",
        f"signed by {', '.join(claims.identities) or 'no identity'}, which is not a workflow of publisher "
        f"{publisher.id} ({workflows})",
    )


# Sigstore


def _der_utf8(raw: bytes) -> str:
    """The text of a DER UTF8String."""
    if len(raw) < 2 or raw[0] != 0x0C:
        return raw.decode("utf-8", "replace")
    length, start = raw[1], 2
    if length & 0x80:
        count = length & 0x7F
        length, start = int.from_bytes(raw[2 : 2 + count], "big"), 2 + count
    return raw[start : start + length].decode("utf-8", "replace")


def certificate_claims(cert) -> SigstoreClaims:
    """The OIDC issuer and the identities of a Fulcio certificate (a ``cryptography`` X.509 certificate)."""
    from cryptography import x509

    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        identities = (
            *names.get_values_for_type(x509.UniformResourceIdentifier),
            *names.get_values_for_type(x509.RFC822Name),
        )
    except x509.ExtensionNotFound:
        identities = ()
    issuer = None
    for oid, der in _ISSUER_OIDS:
        try:
            raw = cert.extensions.get_extension_for_oid(x509.ObjectIdentifier(oid)).value.value
        except x509.ExtensionNotFound:
            continue
        issuer = _der_utf8(raw) if der else raw.decode("utf-8", "replace")
        break
    return SigstoreClaims(issuer, tuple(identities))


class ClaimsPolicy:
    """A verification policy of sigstore-python that accepts every certificate and keeps its claims: ``verify``
    matches them against the publisher afterwards, so a signature that does not verify and one by another workflow
    are refused for different reasons."""

    def __init__(self):
        self.claims: SigstoreClaims | None = None

    def verify(self, cert) -> None:
        self.claims = certificate_claims(cert)


class SigstoreVerifier:
    """Checks Sigstore bundles with sigstore-python against the public-good trust root, which the first check fetches
    (``offline`` uses the cached one only). One instance serves a process; checks may run in threads."""

    def __init__(self, *, offline: bool = False, factory: Callable | None = None):
        self.offline = offline
        self._factory = factory
        self._verifier = None
        self._lock = threading.Lock()

    def _get(self):
        with self._lock:
            if self._verifier is None:
                try:
                    if self._factory is None:
                        from sigstore.verify import Verifier

                        self._verifier = Verifier.production(offline=self.offline)
                    else:
                        self._verifier = self._factory()
                except Exception as exc:  # TUF, the network or the cache: the check cannot run
                    raise SignatureUnavailable(
                        f"cannot load Sigstore's trust root ({type(exc).__name__}: {exc}); try again shortly"
                    ) from None
            return self._verifier

    def __call__(self, data: bytes, bundle: dict) -> SigstoreClaims:
        try:
            from sigstore.errors import VerificationError
            from sigstore.models import Bundle, InvalidBundle
        except ImportError:
            raise SignatureUnavailable(
                "this hub cannot check Sigstore signatures: the sigstore package is not installed (the hub-server "
                "extra brings it)"
            ) from None
        try:
            parsed = Bundle.from_json(json.dumps(bundle))
        except (InvalidBundle, ValueError) as exc:
            raise SignatureError("malformed", f"the signature is not a Sigstore bundle: {exc}") from None
        policy = ClaimsPolicy()
        try:
            self._get().verify_artifact(data, parsed, policy)
        except VerificationError as exc:
            raise SignatureError("invalid", f"the Sigstore signature does not verify: {exc}") from None
        if policy.claims is None:
            raise SignatureError("invalid", "the Sigstore signature does not verify: its certificate was not checked")
        return policy.claims


_DEFAULT: list[SigstoreVerifier] = []


def default_sigstore() -> SigstoreVerifier:
    """The process's one verifier of Sigstore bundles."""
    if not _DEFAULT:
        _DEFAULT.append(SigstoreVerifier())
    return _DEFAULT[0]
