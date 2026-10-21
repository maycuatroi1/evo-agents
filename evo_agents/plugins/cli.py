"""``evo-agents plugin test|pack|sign|keygen``: what the author of a plugin runs on their own machine.

- ``test DIR``: the conformance checks of ``evo_agents.plugins.conformance``; exit 1 when one fails;
- ``pack DIR``: the plugin's deterministic bundle, ``<id>-<version>.tar.gz`` in the current directory or ``--output``,
  and its SHA-256;
- ``sign BUNDLE --key FILE``: an Ed25519 signature of the bundle, written beside it as ``BUNDLE.sig.json``, which
  ``evo-agents hub plugin publish`` picks up. A bundle a GitHub Actions workflow signs with Sigstore needs no command
  here: ``python -m sigstore sign BUNDLE`` writes ``BUNDLE.sigstore.json``;
- ``keygen --output FILE``: a new Ed25519 key, its 32-byte seed in base64 in FILE (mode 0600), and the
  EVO_HUB_PLUGIN_PUBLISHERS entry of its public key.

Signing imports ``cryptography``, which the hub-server extra brings. Standard library otherwise.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from pathlib import Path

from evo_agents.plugins.bundle import BundleError, pack, read_bundle
from evo_agents.plugins.signing import sign_ed25519

SEED_BYTES = 32


def _test(args) -> int:
    from evo_agents.plugins.conformance import run_conformance

    report = run_conformance(args.directory, timeout=args.timeout)
    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
        return 0 if report.ok else 1
    for check in report.checks:
        print(f"{'OK  ' if check.ok else 'FAIL'} {check.name}")
        for detail in check.details:
            print(f"       {detail}")
    failed = [check.name for check in report.checks if not check.ok]
    print(f"{len(report.checks)} check(s), {len(failed)} failed" + (f": {', '.join(failed)}" if failed else ""))
    return 0 if report.ok else 1


def _pack(args) -> int:
    try:
        bundle = pack(args.directory)
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    manifest = bundle.manifest
    output = Path(args.output) if args.output else Path(f"{manifest.id}-{manifest.version}.tar.gz")
    output.write_bytes(bundle.data)
    print(f"{output}  {manifest.id} {manifest.version}  {len(bundle.data)} bytes  sha256 {bundle.sha256}")
    return 0


def _read_seed(path: Path) -> bytes:
    try:
        seed = base64.b64decode(path.read_text(encoding="utf-8").strip(), validate=True)
    except (OSError, binascii.Error, ValueError) as exc:
        raise ValueError(f"cannot read the key in {path}: {exc}") from None
    if len(seed) != SEED_BYTES:
        raise ValueError(f"{path} holds {len(seed)} bytes, not the {SEED_BYTES}-byte seed of an Ed25519 key")
    return seed


def _sign(args) -> int:
    bundle = Path(args.bundle)
    try:
        data = bundle.read_bytes()
        read_bundle(data)
        envelope = sign_ed25519(data, _read_seed(Path(args.key)))
    except (OSError, BundleError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output = Path(args.output) if args.output else Path(f"{bundle}.sig.json")
    output.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    print(f"{output}  ed25519  sha256 {envelope['sha256']}")
    return 0


def _keygen(args) -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    output = Path(args.output)
    if output.exists():
        print(f"error: {output} exists; a key is never overwritten", file=sys.stderr)
        return 1
    seed = os.urandom(SEED_BYTES)
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(base64.b64encode(seed).decode() + "\n")
    print(f"wrote the private key to {output}; keep it out of every repository")
    print(f"EVO_HUB_PLUGIN_PUBLISHERS entry: {args.publisher}=ed25519:{base64.b64encode(public).decode()}")
    return 0


def register(sub) -> None:
    plugin = sub.add_parser("plugin", help="test, pack and sign a plugin on this machine")
    psub = plugin.add_subparsers(dest="command", required=True)
    test = psub.add_parser("test", help="check that a plugin directory speaks plugin/1 and packs into a bundle")
    test.add_argument("directory", metavar="DIR", help="the plugin directory, evo-plugin.json at its top")
    test.add_argument("--timeout", type=float, default=10.0, help="seconds the backend has for each answer")
    test.add_argument("--json", action="store_true", help="machine-readable output")
    test.set_defaults(func=_test)
    packed = psub.add_parser("pack", help="pack a plugin directory into its deterministic bundle")
    packed.add_argument("directory", metavar="DIR")
    packed.add_argument("--output", "-o", metavar="FILE", help="default: <id>-<version>.tar.gz here")
    packed.set_defaults(func=_pack)
    signed = psub.add_parser("sign", help="sign a bundle with an Ed25519 key, beside it as BUNDLE.sig.json")
    signed.add_argument("bundle", metavar="BUNDLE")
    signed.add_argument("--key", required=True, metavar="FILE", help="the key `plugin keygen` wrote")
    signed.add_argument("--output", "-o", metavar="FILE", help="default: BUNDLE.sig.json")
    signed.set_defaults(func=_sign)
    keygen = psub.add_parser("keygen", help="make an Ed25519 key for signing plugins")
    keygen.add_argument("--output", "-o", required=True, metavar="FILE", help="where the private key goes")
    keygen.add_argument("--publisher", default="PUBLISHER", help="the publisher id to print in the entry")
    keygen.set_defaults(func=_keygen)
