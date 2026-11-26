"""``hub canvas export|import``: a canvas as ``evo-canvas/1`` JSON and back, and the JSON files the commands read."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from evo_agents.clikit import Arg, Result, Text, Verbatim, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.canvas.keys import IMPORT_KEYS, PROJECT, SLUG
from evo_agents.hub.cli.canvas.resources import Canvases
from evo_agents.hub.cli.canvas.views import import_view
from evo_agents.hub.cli.features import answer
from evo_agents.hub.client import HubError


def read_json(file: str) -> Any:
    """The JSON of ``file``, ``-`` for stdin; HubError when it cannot be read or is no JSON."""
    try:
        text = sys.stdin.read() if file == "-" else Path(file).expanduser().read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, UnicodeDecodeError) as exc:
        raise HubError(f"cannot read {file}: {exc}") from None
    except ValueError as exc:
        raise HubError(f"{file} is no JSON: {exc}") from None


class CanvasExport(HubCommand):
    name = "export"
    help = "a canvas as evo-canvas/1 JSON"
    arguments = (SLUG, PROJECT, Arg("--output", metavar="FILE", help="write it to FILE instead of stdout"))

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Canvases.of(ctx).export(args.slug)
        text = json.dumps(found, ensure_ascii=False, indent=2) + "\n"
        if not args.output:
            return Result(found, Verbatim(text))
        try:
            Path(args.output).expanduser().write_text(text, encoding="utf-8")
        except OSError as exc:
            raise HubError(f"cannot write {args.output}: {exc}") from None
        return Result(found, Text(f"Wrote canvas {args.slug}, {len(found['claims'])} claims, to {args.output}."))


class CanvasImport(HubCommand):
    name = "import"
    help = "evo-canvas/1 JSON, keeping the claims' codes; all or nothing"
    arguments = (
        Arg("file", metavar="FILE", help="the JSON file; - for stdin"),
        PROJECT,
        Arg("--dry-run", action="store_true", help="print what it would import and write nothing"),
    )
    output = returns_object(*IMPORT_KEYS, schema="CanvasImportResult")

    def run(self, ctx: HubContext) -> Result:
        body = {"data": read_json(ctx.args.file), "dry_run": ctx.args.dry_run}
        return answer(ctx, Canvases.of(ctx).import_(body), import_view)
