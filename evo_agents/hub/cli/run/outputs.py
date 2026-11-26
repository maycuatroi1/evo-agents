"""``evo-agents hub run outputs RUN [--source S] [--download DIR] [--json]``, for ``evo_agents.hub.cli.run``: the files
a run's agent made that the hub keeps (``evo_agents.hub.outputs``), as GET /v1/projects/{p}/runs/{id}/outputs lists
them, oldest first: id, source, name, media type, size and caption.

``--download DIR`` saves each into DIR under its own name (an output of the run's ``.evo-run/outputs/`` keeps its path
below it), through a presigned GET of its bytes the hub hands out (GET .../outputs/{oid}/link), each checked against
its size and SHA-256. A file of that name already in DIR with the same bytes is kept; another one keeps its place, and
the output goes next to it as ``name (2).ext``. A name that would land outside DIR is refused. ``--json`` prints what
the hub answered (``RunOutputs``), after the downloads when there are some. Standard library only, like the rest of
the client.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from evo_agents.clikit import Arg, Result, Table, Text, Views, returns_object
from evo_agents.hub import outputs
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext
from evo_agents.hub.cli.run.options import RUN_ID
from evo_agents.hub.cli.run.resources import Runs
from evo_agents.hub.client import HubError, get_presigned

OUTPUTS_KEYS = ("run_id", "outputs", "count", "bytes", "tool_images")
CAPTION_CHARS = 50  # of a caption in the table
UNITS = ("B", "KiB", "MiB", "GiB")


def size_text(size: int) -> str:
    """``size`` bytes as a person reads them: 512 B, 1.5 KiB, 200.0 MiB."""
    value, unit = float(size), 0
    while value >= 1024 and unit < len(UNITS) - 1:
        value, unit = value / 1024, unit + 1
    return f"{size} B" if unit == 0 else f"{value:.1f} {UNITS[unit]}"


def _caption(output: dict) -> str:
    text = " ".join((output.get("caption") or "-").split())
    return text if len(text) <= CAPTION_CHARS else text[: CAPTION_CHARS - 3] + "..."


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def place(directory: Path, name: str, sha256: str) -> tuple[Path, bool]:
    """(where output ``name`` goes in ``directory``, whether that file holds its bytes already): its own name, or
    ``name (n).ext`` beside a file of that name with other bytes. HubError for a name that leaves ``directory``."""
    root = directory.resolve()
    try:
        target = (root / outputs.check_name(name)).resolve()
    except ValueError as exc:
        raise HubError(f"output {name!r} is not saved: {exc}") from None
    if root not in target.parents:
        raise HubError(f"output {name!r} would be saved outside {directory}: not saved")
    stem, suffix, number = target.stem, target.suffix, 2
    candidate = target
    while candidate.exists():
        if candidate.is_file() and _digest(candidate) == sha256:
            return candidate, True
        candidate = target.with_name(f"{stem} ({number}){suffix}")
        number += 1
    return candidate, False


def download(runs: Runs, run_id: int, outputs_listed: list[dict], directory: Path) -> list[tuple[str, Path, bool]]:
    """Save each output of run ``run_id`` into ``directory`` (see the module): (name, path, whether it was there
    already) of each."""
    directory.mkdir(parents=True, exist_ok=True)
    saved = []
    for output in outputs_listed:
        target, held = place(directory, output["name"], output["sha256"])
        if not held:
            link = runs.output_link(run_id, output["id"])
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            digest = get_presigned(link["url"], partial, output["size"])
            if digest != output["sha256"]:
                partial.unlink(missing_ok=True)
                raise HubError(f"output {output['id']} came with other bytes than its SHA-256: not saved")
            partial.replace(target)
        saved.append((output["name"], target, held))
    return saved


class RunOutputs(HubCommand):
    name = "outputs"
    help = (
        "the files a run's agent made that the hub keeps (sent, tool images, .evo-run/outputs/); --download saves them"
    )
    arguments = (
        RUN_ID,
        PROJECT,
        Arg(
            "--source",
            action="append",
            choices=outputs.SOURCES,
            metavar="SOURCE",
            help=f"only outputs of this source, one of {', '.join(outputs.SOURCES)}; repeat it for several",
        ),
        Arg(
            "--download",
            metavar="DIR",
            help="save each output into DIR under its own name, checked against its SHA-256",
        ),
    )
    output = returns_object(*OUTPUTS_KEYS, schema="RunOutputs")
    columns = ("ID", "SOURCE", "NAME", "TYPE", "SIZE", "CAPTION")

    def run(self, ctx: HubContext) -> Result:
        args, runs = ctx.args, Runs.of(ctx)
        found = runs.outputs(args.run, args.source or ())
        saved = download(runs, args.run, found["outputs"], Path(args.download)) if args.download else []
        rows = [
            (o["id"], o["source"], o["name"], o["media_type"], size_text(o["size"]), _caption(o))
            for o in found["outputs"]
        ]
        none = f"Run #{args.run} has no outputs" + (" of these sources." if args.source else ".")
        lines = [f"{found['count']} output(s), {size_text(found['bytes'])}, on the hub for run #{args.run}."]
        lines += [f"{'kept' if held else 'saved'} {name} -> {target}" for name, target, held in saved]
        return Result(found, Views(Table(self.columns, rows, none), Text(*lines)))
