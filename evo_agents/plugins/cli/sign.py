"""``evo-agents plugin sign BUNDLE --key FILE``: an Ed25519 signature of the bundle, written beside it as
``BUNDLE.sig.json``, which ``evo-agents hub plugin publish`` picks up. A bundle a GitHub Actions workflow signs with
Sigstore needs no command here: ``python -m sigstore sign BUNDLE`` writes ``BUNDLE.sigstore.json``."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from evo_agents.clikit import Arg, Context, Result, Text
from evo_agents.plugins.bundle import BundleError, read_bundle
from evo_agents.plugins.cli.base import PluginCommand, Refused
from evo_agents.plugins.signing import sign_ed25519

SEED_BYTES = 32


def read_seed(path: Path) -> bytes:
    """The 32-byte seed of the Ed25519 key `plugin keygen` wrote in ``path``; ValueError when it holds none."""
    try:
        seed = base64.b64decode(path.read_text(encoding="utf-8").strip(), validate=True)
    except (OSError, binascii.Error, ValueError) as exc:
        raise ValueError(f"cannot read the key in {path}: {exc}") from None
    if len(seed) != SEED_BYTES:
        raise ValueError(f"{path} holds {len(seed)} bytes, not the {SEED_BYTES}-byte seed of an Ed25519 key")
    return seed


class Sign(PluginCommand):
    name = "sign"
    help = "sign a bundle with an Ed25519 key, beside it as BUNDLE.sig.json"
    arguments = (
        Arg("bundle", metavar="BUNDLE"),
        Arg("--key", required=True, metavar="FILE", help="the key `plugin keygen` wrote"),
        Arg("--output", "-o", metavar="FILE", help="default: BUNDLE.sig.json"),
    )

    def run(self, ctx: Context) -> Result:
        bundle = Path(ctx.args.bundle)
        try:
            data = bundle.read_bytes()
            read_bundle(data)
            envelope = sign_ed25519(data, read_seed(Path(ctx.args.key)))
        except (OSError, BundleError, ValueError) as exc:
            raise Refused(str(exc)) from None
        output = Path(ctx.args.output) if ctx.args.output else Path(f"{bundle}.sig.json")
        output.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
        return Result(envelope, Text(f"{output}  ed25519  sha256 {envelope['sha256']}"))


SIGN = Sign()
