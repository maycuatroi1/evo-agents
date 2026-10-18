"""Plugins the tests build: a manifest, a plugin directory and its bundle, Ed25519 keys of a publisher, and a stand-in
for Sigstore that says what a certificate would.

``FakeSigstore`` takes the place of ``evo_agents.plugins.signing.SigstoreVerifier``: the "Sigstore bundle" of a
``sigstore_signature`` is a dict naming the certificate's issuer and identity, and whether the signature verifies. The
real check against Sigstore's trust root needs a bundle that a GitHub Actions workflow signed, which no test here can
make offline.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from evo_agents.plugins.bundle import Bundle, pack
from evo_agents.plugins.signing import GITHUB_ISSUER, SignatureError, SigstoreClaims, sign_ed25519

PUBLISHER = "acme"
REPOSITORY = "acme/evo-plugin-sandbox"
WORKFLOW = f"{REPOSITORY}/.github/workflows/release.yml"
SIGNER = f"https://github.com/{WORKFLOW}@refs/heads/main"
SEED = bytes(range(32))  # the private key of PUBLISHER in the tests
OTHER_SEED = bytes(range(1, 33))


def manifest(**changes) -> dict:
    """A manifest of plugin sandbox 1.0.0 of PUBLISHER, with ``changes``; a change to None leaves the key out."""
    data = {
        "id": "sandbox",
        "version": "1.0.0",
        "publisher": PUBLISHER,
        "name": "Sandbox",
        "description": "A plugin the tests publish",
        "engines": {"evo-agents": ">=0.16"},
        "plugin_api": 1,
        "permissions": ["runs:read", "chats:write"],
        "config": {
            "type": "object",
            "properties": {"greeting": {"type": "string", "title": "Greeting"}, "token": {"x-secret": True}},
            "required": ["greeting"],
        },
        "contributes": {"routes": [{"name": "hello", "title": "Hello"}], "jobs": [{"name": "tick"}]},
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


def write_plugin(directory: Path, data: dict | None = None, files: dict[str, str] | None = None) -> Path:
    """A plugin directory with evo-plugin.json holding ``data`` (``manifest()`` by default) and ``files``."""
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest() if data is None else data, indent=2)
    (directory / "evo-plugin.json").write_text(text, encoding="utf-8")
    for path, content in (files or {"web/index.html": "<p>hello</p>\n"}).items():
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_text(content, encoding="utf-8")
    return directory


def bundle_of(base: Path, **changes) -> Bundle:
    """The bundle of a plugin directory under ``base`` whose manifest is ``manifest(**changes)``."""
    data = manifest(**changes)
    return pack(write_plugin(base / f"{data['id']}-{data['version']}", data))


def public_key(seed: bytes = SEED) -> str:
    """The Ed25519 public key of ``seed``, 32 bytes in base64, as EVO_HUB_PLUGIN_PUBLISHERS takes it."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def ed25519_publishers(seed: bytes = SEED, publisher: str = PUBLISHER) -> str:
    return f"{publisher}=ed25519:{public_key(seed)}"


def ed25519_signature(data: bytes, seed: bytes = SEED) -> dict:
    return sign_ed25519(data, seed)


def sigstore_signature(data: bytes, *, identity: str = SIGNER, issuer: str = GITHUB_ISSUER, valid: bool = True) -> dict:
    """The envelope of a stand-in Sigstore bundle of ``data`` that FakeSigstore reads."""
    fake = {"fake": True, "issuer": issuer, "identity": identity, "valid": valid}
    return {"kind": "sigstore", "sha256": hashlib.sha256(data).hexdigest(), "bundle": fake}


class FakeSigstore:
    """A Sigstore check that believes the stand-in bundle: its claims, or ``invalid`` when it says it does not
    verify. ``calls`` counts the checks."""

    def __init__(self):
        self.calls = 0

    def __call__(self, data: bytes, bundle: dict) -> SigstoreClaims:
        self.calls += 1
        if not bundle.get("valid"):
            raise SignatureError("invalid", "the Sigstore signature does not verify: Signature is invalid for input")
        return SigstoreClaims(bundle.get("issuer"), (bundle["identity"],))
