"""``evo-agents hub context answers``: the confirmed answers loaded into the prompts of a project's runs, by week (plan
active-context-answers), over GET /v1/projects/{p}/context-answers.

It prints, for each of the last ``--weeks`` weeks (8 by default, the current one included, weeks start on Monday, UTC),
the runs queued, the runs that loaded at least one answer and the answers loaded, to set the weeks after the policy
revision that turned ``context.answers`` on against those before it (``evo-agents hub policy history``). Every member of
the project reads it. The project is ``--project``, or the ``hub.project`` of the harness around the current directory.
``--json`` prints what the hub answered. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.cli_loop import _project
from evo_agents.hub.cli_policy import PROJECT_HELP
from evo_agents.hub.contract import json_option, returns_object

ANSWER_KEYS = ("project", "enabled", "since", "until", "weeks")


@client_command
def cmd_answers(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    query = urlencode({"weeks": args.weeks}) if args.weeks else ""
    found = hub.call("GET", f"/v1/projects/{quote(project, safe='')}/context-answers" + (f"?{query}" if query else ""))
    if args.json:
        print_json(found)
        return 0
    state = "on" if found["enabled"] else "off"
    print(f"Confirmed answers loaded in project {found['project']} (context.answers is {state}), by week from Monday:")
    rows = [(when(w["start"])[:10], w["runs"], w["runs_with_loads"], w["answers"]) for w in found["weeks"]]
    table(("WEEK", "RUNS", "RUNS WITH ANSWERS", "ANSWERS"), rows)
    return 0


def register_context(hsub) -> None:
    context = hsub.add_parser(
        "context", help="what the hub puts into the prompt of a run: the confirmed answers loaded"
    )
    csub = context.add_subparsers(dest="context_command", required=True)
    answers = csub.add_parser(
        "answers", help="the runs, the runs that loaded a confirmed answer and the answers, by week"
    )
    answers.add_argument("--weeks", type=int, metavar="N", help="the last N weeks, 8 when left out, 52 at most")
    answers.add_argument("--project", help=PROJECT_HELP)
    json_option(answers, returns_object(*ANSWER_KEYS, schema="ContextAnswerWeeks"))
    answers.set_defaults(func=cmd_answers)
