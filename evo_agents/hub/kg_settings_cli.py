"""``evo-agents hub kg settings``: show, set or undo the KG settings of a hub project (``evo_agents.kg.settings``).

``--set KEY=VALUE`` (repeatable) puts keys on top of what the project has, with ``--reason`` and ``--figures`` (a json
object) kept in its change list; ``--undo`` puts back the newest change that is not undone. A key that acts on a build
waits for the next build and is named in ``needs_rebuild``. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.kg import settings as kg_settings


def _values(items: list[str]) -> dict:
    changes = {}
    for item in items:
        key, sep, text = item.partition("=")
        if not sep:
            raise kg_settings.SettingsError(f"--set {item!r}: write KEY=VALUE")
        changes[key.strip()] = kg_settings.parse_value(key.strip(), text)
    return changes


def _figures(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        found = json.loads(text)
    except ValueError:
        raise ValueError("--figures must be a json object") from None
    if not isinstance(found, dict):
        raise ValueError("--figures must be a json object")
    return found


def settings_json(project: str, found: dict) -> dict:
    """What ``--json`` prints, from the hub's answer."""
    return {
        "project": project,
        "settings": found["values"],
        "overrides": found["overrides"],
        "needs_rebuild": found["needs_rebuild"],
        "changes": found["changes"],
    }


@_client_command
def cmd_settings(args) -> int:
    hub, _ = _signed_in()
    base = f"/v1/kg/{quote(args.project, safe='')}/settings"
    try:
        if args.undo:
            found = hub.call("POST", f"{base}/undo", {"reason": args.reason})
        elif args.set:
            body = {"values": _values(args.set), "reason": args.reason, "figures": _figures(args.figures)}
            found = hub.call("PUT", base, body)
        else:
            found = hub.call("GET", base)
    except (kg_settings.SettingsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = settings_json(args.project, found)
    if args.json:
        _print_json(out)
        return 0
    for key, value in out["settings"].items():
        print(f"{key} = {value}" + ("  (set)" if key in out["overrides"] else ""))
    if out["needs_rebuild"]:
        print(f"takes effect with the next build: {', '.join(out['needs_rebuild'])}")
    for change in out["changes"][:5]:
        who = change["changed_by"] or (f"run {change['run_id']}" if change["run_id"] else "-")
        print(f"change {change['id']} ({change['kind']}, {who}): {change['reason'] or 'no reason given'}")
    return 0


def measure_json(project: str, found: dict) -> dict:
    """What ``hub kg measure --json`` prints, from the hub's answer."""
    keys = ("build_id", "split", "questions", "current", "candidate", "candidate_settings", "needs_rebuild", "why")
    return {"project": project, **{k: found.get(k) for k in keys}}


@_client_command
def cmd_measure(args) -> int:
    hub, _ = _signed_in()
    try:
        values = json.loads(Path(args.settings).read_text(encoding="utf-8")) if args.settings else {}
        if not isinstance(values, dict):
            raise ValueError("the settings file must hold a json object")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    body = {"values": values, "split": args.split, "max_questions": args.max_questions}
    found = hub.call("POST", f"/v1/kg/{quote(args.project, safe='')}/measure", body)
    out = measure_json(args.project, found)
    if args.json:
        _print_json(out)
        return 0
    if out["why"]:
        print(f"nothing measured: {out['why']}")
    else:
        print(f"{out['questions']} {out['split']} questions on build {out['build_id']}")
        print(f"current:   {out['current']}")
        print(f"candidate: {out['candidate']}")
    if out["needs_rebuild"]:
        print(f"not measured, they need a build: {', '.join(out['needs_rebuild'])}")
    return 0


def register(ksub) -> None:
    parser = ksub.add_parser(
        "settings", help="show, set (--set KEY=VALUE) or undo (--undo) the KG settings of a hub project"
    )
    parser.add_argument("--project", required=True, help="project name on the hub")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE", help="a key and its new value; repeatable")
    parser.add_argument("--undo", action="store_true", help="put back the newest change that is not undone")
    parser.add_argument("--reason", help="why, kept with the change")
    parser.add_argument("--figures", help="a json object of the numbers the change rests on")
    json_option(parser, returns_object("project", "settings", "overrides", "needs_rebuild", "changes"))
    parser.set_defaults(func=cmd_settings)

    measure = ksub.add_parser(
        "measure", help="score a candidate of the KG settings on the project's question set, changing nothing"
    )
    measure.add_argument("--project", required=True, help="project name on the hub")
    measure.add_argument("--settings", help="a json file of keys and values, tried on top of the project's settings")
    measure.add_argument("--split", choices=("tune", "holdout"), default="tune", help="the part of the set asked")
    measure.add_argument("--max-questions", type=int, default=300, help="at most this many questions")
    json_option(
        measure,
        returns_object(
            "project",
            "build_id",
            "split",
            "questions",
            "current",
            "candidate",
            "candidate_settings",
            "needs_rebuild",
            "why",
        ),
    )
    measure.set_defaults(func=cmd_measure)
