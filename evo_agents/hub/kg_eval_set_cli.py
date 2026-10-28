"""``evo-agents hub kg eval-set``: the retrieval question set of a project, versioned on the hub.

``eval-set push FILE --project P`` puts a set file of ``evo-agents kg eval`` (docs/kg-eval.md) on the hub (PUT
/v1/kg/{project}/eval-set, the writer role): the next version, or the latest one again when its questions are the
same. Every question needs an id there, since the id fixes its part, tune or holdout. ``eval-set pull --project P``
writes the latest version (``--version N`` another) as a set file, header line first, to ``-o FILE`` or the standard
output: ``--split tune`` for tuning, ``--split holdout`` for the list of holdout questions, which never carry their
answers off the hub. ``--json`` prints the hub's answer. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in
from evo_agents.hub.contract import JsonOutput, json_option, returns_object

EVAL_SET_VIEW = ("set", "version", "digest", "created_at", "split", "answers", "questions")
EVAL_SET_PUT = ("set", "version", "digest", "questions", "holdout", "created")


def _path(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}/eval-set"


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


def _push(hub, args) -> int:
    try:
        name, entries = read_set_file(args.file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    name = args.set or name
    if not name:
        print('error: name the set with --set or a header line {"set": NAME}', file=sys.stderr)
        return 2
    found = hub.call("PUT", _path(args.project), {"set": name, "questions": entries})
    if args.json:
        _print_json(found)
        return 0
    state = "put as version" if found["created"] else "unchanged: still version"
    print(
        f"set {found['set']} of project {args.project} {state} {found['version']}: {found['questions']} questions,"
        f" {found['holdout']} in holdout"
    )
    return 0


def _pull(hub, args) -> int:
    params = {"split": args.split, "answers": "false" if args.no_answers else "true"}
    if args.version:
        params["version"] = args.version
    found = hub.call("GET", f"{_path(args.project)}?{urlencode(params)}")
    if args.json:
        _print_json(found)
        return 0
    text = "".join(line + "\n" for line in set_lines(found))
    if args.output:
        Path(args.output).expanduser().write_text(text, encoding="utf-8")
        print(
            f"wrote {len(found['questions'])} questions of set {found['set']} version {found['version']}"
            f" ({found['split']}) to {args.output}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)
    return 0


@_client_command
def cmd_eval_set(args) -> int:
    if args.action == "push" and not args.file:
        print("error: eval-set push needs the set FILE", file=sys.stderr)
        return 2
    if args.action == "pull" and args.file:
        print("error: eval-set pull takes no FILE; write it with -o FILE", file=sys.stderr)
        return 2
    hub, _ = _signed_in()
    return _push(hub, args) if args.action == "push" else _pull(hub, args)


def register(ksub) -> None:
    from evo_agents.hub.cli import _positive

    parser = ksub.add_parser(
        "eval-set", help="put a project's retrieval question set on the hub, or pull it (tune, or holdout unanswered)"
    )
    parser.add_argument("action", choices=["push", "pull"], help="push a set file, or pull the hub's set")
    parser.add_argument("file", metavar="FILE", nargs="?", help="with push: the set file (JSONL, docs/kg-eval.md)")
    parser.add_argument("--project", required=True, help="project name on the hub")
    parser.add_argument("--set", help="with push: the set's name, when the file has no header line")
    parser.add_argument("--version", type=_positive, help="with pull: this version, not the latest")
    parser.add_argument(
        "--split",
        choices=["all", "tune", "holdout"],
        default="all",
        help="with pull: only this part (default all; holdout questions come without answers)",
    )
    parser.add_argument("--no-answers", action="store_true", help="with pull: no question carries its answers")
    parser.add_argument("-o", "--output", metavar="FILE", help="with pull: write the set here, not to stdout")
    json_option(
        parser,
        JsonOutput(
            "object",
            EVAL_SET_VIEW,
            "EvalSetView",
            variants=(("push", returns_object(*EVAL_SET_PUT, schema="EvalSetPut")),),
        ),
        help="machine-readable output: the hub's set, or with push the version it was put as",
    )
    parser.set_defaults(func=cmd_eval_set)
