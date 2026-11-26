"""``evo-agents kg bind``: bind a directory to one project explicitly, list the bindings, or remove one. Bindings live
in bound.json beside the projects.json index and win over the harness and the index (``evo_agents.kg.project``)."""

from __future__ import annotations

import argparse
from pathlib import Path

from evo_agents.clikit import Arg, Result, Text, UsageError
from evo_agents.kg.cli.base import JSON, Failed, KgCommand, KgContext, project_failed, shown
from evo_agents.kg.project import ProjectError, bind, read_bindings, unbind


class KgBind(KgCommand):
    name = "bind"
    help = "bind a directory and everything below it to one project"
    arguments = (
        Arg("directory", nargs="?", help="directory to bind (default: the cwd)"),
        Arg("--project", help="project name or harness path (default: the project the directory resolves to now)"),
        Arg("--list", action="store_true", help="print every binding"),
        Arg("--remove", metavar="DIR", help="delete the binding of DIR"),
        JSON,
    )

    def check(self, args: argparse.Namespace) -> None:
        if (args.list or args.remove) and (args.project or args.directory or (args.list and args.remove)):
            raise UsageError("--list and --remove DIR take no other arguments")

    def run(self, ctx: KgContext) -> Result:
        args = ctx.args
        if args.list:
            bindings = read_bindings()
            lines = [
                f"{directory}  ->  {entry.get('project')}  ({entry.get('harness_root')})"
                for directory, entry in sorted(bindings.items())
            ]
            return shown(ctx, bindings, Text(*lines) if lines else Text("no bindings"))
        if args.remove:
            removed = unbind(args.remove)
            if removed is None:
                raise Failed(f"{args.remove} has no binding (see evo-agents kg bind --list)")
            return shown(ctx, {"removed": removed}, Text(f"unbound {removed}"))
        directory = Path(args.directory or Path.cwd()).expanduser().resolve()
        try:
            project = bind(directory, args.project)
        except ProjectError as exc:
            raise project_failed(exc) from exc
        found = {"directory": str(directory), "project": project.name, "harness_root": str(project.harness.root)}
        return shown(ctx, found, Text(f"bound {directory}  ->  {project.name}  ({project.harness.root})"))


BIND = KgBind()
