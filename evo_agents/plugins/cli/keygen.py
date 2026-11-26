"""``evo-agents plugin keygen --output FILE``: a new Ed25519 key, its 32-byte seed in base64 in FILE (mode 0600), and
the EVO_HUB_PLUGIN_PUBLISHERS entry of its public key. It imports ``cryptography``, which the hub-server extra
brings."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from evo_agents.clikit import Arg, Context, Result, Text
from evo_agents.plugins.cli.base import PluginCommand, Refused
from evo_agents.plugins.cli.sign import SEED_BYTES


class Keygen(PluginCommand):
    name = "keygen"
    help = "make an Ed25519 key for signing plugins"
    arguments = (
        Arg("--output", "-o", required=True, metavar="FILE", help="where the private key goes"),
        Arg("--publisher", default="PUBLISHER", help="the publisher id to print in the entry"),
    )

    def run(self, ctx: Context) -> Result:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        output = Path(ctx.args.output)
        if output.exists():
            raise Refused(f"{output} exists; a key is never overwritten")
        seed = os.urandom(SEED_BYTES)
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(base64.b64encode(seed).decode() + "\n")
        entry = f"{ctx.args.publisher}=ed25519:{base64.b64encode(public).decode()}"
        return Result(
            None,
            Text(
                f"wrote the private key to {output}; keep it out of every repository",
                f"EVO_HUB_PLUGIN_PUBLISHERS entry: {entry}",
            ),
        )


KEYGEN = Keygen()
