"""``evo-agents hub context answers``: the confirmed answers loaded into the prompts of a project's runs, by week (plan
active-context-answers), over GET /v1/projects/{p}/context-answers.

It prints, for each of the last ``--weeks`` weeks (8 by default, the current one included, weeks start on Monday, UTC),
the runs queued, the runs that loaded at least one answer and the answers loaded, to set the weeks after the policy
revision that turned ``context.answers`` on against those before it (``evo-agents hub policy history``). Every member of
the project reads it. The project is ``--project``, or the ``hub.project`` of the harness around the current directory.
``--json`` prints what the hub answered. Standard library only, like the rest of the client.

The command is a class of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`ContextAnswers`
is the gateway to the route.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, when

ANSWER_KEYS = ("project", "enabled", "since", "until", "weeks")


class ContextAnswers(ProjectResource):
    """The confirmed answers loaded in a project's runs, by week: GET /v1/projects/{p}/context-answers (Gateway)."""

    collection = "context-answers"

    def weeks(self, weeks: int | None) -> dict:
        return self.call("GET", weeks=weeks or None)


class Answers(HubCommand):
    name = "answers"
    help = "the runs, the runs that loaded a confirmed answer and the answers, by week"
    arguments = (
        Arg("--weeks", type=int, metavar="N", help="the last N weeks, 8 when left out, 52 at most"),
        PROJECT,
    )
    output = returns_object(*ANSWER_KEYS, schema="ContextAnswerWeeks")
    columns = ("WEEK", "RUNS", "RUNS WITH ANSWERS", "ANSWERS")

    def run(self, ctx: HubContext) -> Result:
        found = ContextAnswers.of(ctx).weeks(ctx.args.weeks)
        state = "on" if found["enabled"] else "off"
        said = (
            f"Confirmed answers loaded in project {found['project']} (context.answers is {state}), by week from Monday:"
        )
        rows = [(when(w["start"])[:10], w["runs"], w["runs_with_loads"], w["answers"]) for w in found["weeks"]]
        return Result(found, Views(Text(said), Table(self.columns, rows, bare=True)))


CONTEXT = Group(  # hub context
    "context", "what the hub puts into the prompt of a run: the confirmed answers loaded", [Answers()]
)
