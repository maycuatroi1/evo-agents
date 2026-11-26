"""``evo-agents plugin pack DIR``: the plugin's deterministic bundle, ``<id>-<version>.tar.gz`` in the current
directory or ``--output``, and its SHA-256."""

from __future__ import annotations

from pathlib import Path

from evo_agents.clikit import Arg, Context, Result, Text
from evo_agents.plugins.bundle import BundleError, pack
from evo_agents.plugins.cli.base import PluginCommand, Refused


class Pack(PluginCommand):
    name = "pack"
    help = "pack a plugin directory into its deterministic bundle"
    arguments = (
        Arg("directory", metavar="DIR"),
        Arg("--output", "-o", metavar="FILE", help="default: <id>-<version>.tar.gz here"),
    )

    def run(self, ctx: Context) -> Result:
        try:
            bundle = pack(ctx.args.directory)
        except BundleError as exc:
            raise Refused(str(exc)) from None
        manifest = bundle.manifest
        output = Path(ctx.args.output) if ctx.args.output else Path(f"{manifest.id}-{manifest.version}.tar.gz")
        output.write_bytes(bundle.data)
        return Result(
            None, Text(f"{output}  {manifest.id} {manifest.version}  {len(bundle.data)} bytes  sha256 {bundle.sha256}")
        )


PACK = Pack()
