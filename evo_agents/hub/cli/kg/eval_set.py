"""``evo-agents hub kg eval-set``: the retrieval question set of a project, versioned on the hub.

``eval-set push FILE --project P`` puts a set file of ``evo-agents kg eval`` (docs/kg-eval.md) on the hub (PUT
/v1/kg/{project}/eval-set, the writer role): the next version, or the latest one again when its questions are the
same. Every question needs an id there, since the id fixes its part, tune or holdout. ``eval-set pull --project P``
writes the latest version (``--version N`` another) as a set file, header line first, to ``-o FILE`` or the standard
output: ``--split tune`` for tuning, ``--split holdout`` for the list of holdout questions, which never carry their
answers off the hub. ``--json`` prints the hub's answer. Standard library only.

:data:`EVAL_SET` is a class command of ``evo_agents.clikit``; :class:`KgEvalSet` is the gateway to its route.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evo_agents.clikit import NOTHING, Arg, JsonOutput, Result, Stderr, Text, UsageError, Verbatim, View, returns_object
from evo_agents.hub.cli import positive
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource

EVAL_SET_VIEW = ("set", "version", "digest", "created_at", "split", "answers", "questions")
EVAL_SET_PUT = ("set", "version", "digest", "questions", "holdout", "created")


class KgEvalSet(KgResource):
    """The retrieval question set of a project: GET and PUT /v1/kg/{p}/eval-set."""

    collection = "eval-set"

    def put(self, name: str, questions: list[dict]) -> dict:
        return self.call("PUT", body={"set": name, "questions": questions})

    def get(self, split: str, answers: bool, version: int | None = None) -> dict:
        return self.call("GET", split=split, answers="true" if answers else "false", version=version or None)


def read_set_file(path: str) -> tuple[str | None, list[dict]]:
    """(the header's set name, the question objects) of a set file; ValueError names a line that is no object."""
    from evo_agents.kg.evaluate import is_header

    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror or exc}") from None
    name, entries = None, []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} line {number}: not JSON ({exc.msg})") from None
        if not entries and name is None and is_header(entry):
            name = entry.get("set")
            continue
        entries.append(entry)
    return name, entries


def set_lines(view: dict) -> list[str]:
    """The set file of a GET answer: its header, then a question a line (without the split the hub adds)."""
    header = {"set": view["set"], "version": view["version"]}
    questions = [{k: v for k, v in q.items() if k != "split"} for q in view["questions"]]
    return [json.dumps(entry, ensure_ascii=False) for entry in [header, *questions]]


def pushed_line(project: str, found: dict) -> str:
    state = "put as version" if found["created"] else "unchanged: still version"
    return (
        f"set {found['set']} of project {project} {state} {found['version']}: {found['questions']} questions,"
        f" {found['holdout']} in holdout"
    )


def pulled_view(found: dict, output: str | None) -> View:
    """The set file of a pull on stdout, or written to ``output`` with a line on stderr that says so."""
    text = "".join(line + "\n" for line in set_lines(found))
    if not output:
        return Verbatim(text)
    Path(output).expanduser().write_text(text, encoding="utf-8")
    return Stderr(
        Text(
            f"wrote {len(found['questions'])} questions of set {found['set']} version {found['version']}"
            f" ({found['split']}) to {output}"
        )
    )


class KgEvalSetCommand(HubCommand):
    name = "eval-set"
    help = "put a project's retrieval question set on the hub, or pull it (tune, or holdout unanswered)"
    arguments = (
        Arg("action", choices=["push", "pull"], help="push a set file, or pull the hub's set"),
        Arg("file", metavar="FILE", nargs="?", help="with push: the set file (JSONL, docs/kg-eval.md)"),
        KG_PROJECT,
        Arg("--set", help="with push: the set's name, when the file has no header line"),
        Arg("--version", type=positive, help="with pull: this version, not the latest"),
        Arg(
            "--split",
            choices=["all", "tune", "holdout"],
            default="all",
            help="with pull: only this part (default all; holdout questions come without answers)",
        ),
        Arg("--no-answers", action="store_true", help="with pull: no question carries its answers"),
        Arg("-o", "--output", metavar="FILE", help="with pull: write the set here, not to stdout"),
    )
    output = JsonOutput(
        "object",
        EVAL_SET_VIEW,
        "EvalSetView",
        variants=(("push", returns_object(*EVAL_SET_PUT, schema="EvalSetPut")),),
    )
    json_help = "machine-readable output: the hub's set, or with push the version it was put as"

    def check(self, args: argparse.Namespace) -> None:
        if args.action == "push" and not args.file:
            raise UsageError("eval-set push needs the set FILE")
        if args.action == "pull" and args.file:
            raise UsageError("eval-set pull takes no FILE; write it with -o FILE")

    def run(self, ctx: HubContext) -> Result:
        args, sets = ctx.args, KgEvalSet.of(ctx)
        if args.action == "pull":
            found = sets.get(args.split, not args.no_answers, args.version)
            return Result(found, NOTHING if args.json else pulled_view(found, args.output))
        try:
            name, entries = read_set_file(args.file)
        except ValueError as exc:
            raise UsageError(str(exc)) from None
        name = args.set or name
        if not name:
            raise UsageError('name the set with --set or a header line {"set": NAME}')
        found = sets.put(name, entries)
        return Result(found, Text(pushed_line(ctx.project, found)))


EVAL_SET = KgEvalSetCommand()
NODES = (EVAL_SET,)
