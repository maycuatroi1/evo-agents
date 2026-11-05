"""The kg_init run of a project on the hub, without the database (plan kg-data-profile-curator, criterion a5): when
one is asked for, what the Curator reads, and the report. ``evo_agents.hub.server.kg_init`` holds the routes and the
phases; ``evo_agents.kg.init_run`` holds what the hub and a machine share (the profile's change, the prompt, the decision).

A run is asked for by:

- the first successful build of a project with a knowledge config (``first_build``);
- ``evo-agents hub kg init --project P`` (``manual``; ``--all`` for a hub admin);
- a profile that changed a lot since the profile of the last run (``profile``): a source was added, a language or data
  type reached 10% of the content lines, or the content lines changed by half or more.

A project that has no run is not asked about its profile: a project that existed before kg_init gets its first run when
called for. A run goes to the project's default agent pool as a review run with lens ``kg_init``, under the charter's
review cost cap, or HUB_CAP_USD without a charter; no night shift is needed.
"""

from __future__ import annotations

from collections.abc import Mapping

from evo_agents.kg import init_run

HUB_CAP_USD = 2.0  # the cost cap of a run of a project without a charter
HUB_MAX_TURNS = 60
HUB_MAX_SECONDS = 3600
MAX_LISTED = 10
ERROR_CHARS = 2000

FIRST_BUILD = "the first successful build of a project with a knowledge config"
CALLED = "asked for by {who}"

TOOLS = (
    "## How to record your decision\n"
    "Write the JSON object to a file and run `evo-agents hub kg init --project {project} --decide FILE` "
    "(run #{run}). The hub validates it, applies the strategy and the settings with your reasons and figures, and "
    "keeps the rest in the run. Sources and code changes you decided go through the proposal flow: file each one "
    "with kg_propose (kind kg_source for a source) or `evo-agents hub curator propose --lens kg_init` and name the "
    "proposal in `reasons`. Read the previous optimize loop's figures below before you keep or revert its changes."
)


def why_manual(login: str, all_projects: bool = False) -> list[str]:
    return [CALLED.format(who=login) + (" for every project" if all_projects else "")]


def why_changed(before: Mapping | None, after: Mapping | None) -> list[str]:
    """What changed in the profile since the last run's, as ``init_run.changed`` says it; empty when nothing did."""
    return init_run.changed(before, after)


def wants_run(
    *, has_config: bool, first_build: bool, last_profile: Mapping | None, profile: Mapping | None
) -> list[str]:
    """The reasons to queue a run now for a project after a successful build whose profile is ``profile``: the first
    build of a project with a config, else what changed since the last run's profile; no reasons without a profile or
    for a project that never had a run (``last_profile`` None, not first)."""
    if not has_config or not profile:
        return []
    if first_build:
        return [FIRST_BUILD]
    if last_profile is None:
        return []
    return why_changed(last_profile, profile)


def budget(charter: Mapping | None, gate_budget: Mapping | None = None) -> dict:
    """The caps of the run: the charter's review cost cap under the next run's caps, else the hub's defaults."""
    charter = charter or {}
    cap = (charter.get("review") or {}).get("budget_usd")
    base = dict(gate_budget or {})
    usd = base.get("max_usd", HUB_CAP_USD)
    if cap is not None:
        usd = min(usd, cap)
    return {
        "max_usd": round(float(usd), 4),
        "max_turns": base.get("max_turns", HUB_MAX_TURNS),
        "max_seconds": base.get("max_seconds", HUB_MAX_SECONDS),
    }


def previous_optimize(body: Mapping | None) -> list[str]:
    """The figures the optimize loop left for the next run (``body["optimize"]``: before and after, each with the
    holdout's figures), as lines of the prompt; the Curator decides to keep or revert."""
    found = (body or {}).get("optimize") if isinstance(body, Mapping) else None
    if not isinstance(found, Mapping) or not found:
        return []
    lines = ["## Last optimize run: figures before and after (you decide to keep or to revert)"]
    for key in ("before", "after"):
        figures = found.get(key)
        if isinstance(figures, Mapping):
            lines.append(f"- {key}: {init_run._figure(figures)}")
    for change in found.get("changes") or []:
        lines.append(f"- changed: {change}")
    return lines


def prompt(
    *,
    project: str,
    run_id: int,
    why: list[str],
    profile: Mapping,
    coverage: Mapping | None,
    strategies,
    measures: Mapping | None,
    settings: Mapping,
    samples=(),
    optimize: Mapping | None = None,
) -> str:
    """The text the Curator's run starts with: ``init_run.prompt_lines`` with the commands of the hub and the figures the
    optimize loop left. Item text is limited to ``samples``, names the caller cleared for the run's sink."""
    from evo_agents.kg import profile as kg_profile

    tools = TOOLS.format(project=project, run=run_id)
    extra = previous_optimize({"optimize": optimize} if optimize else None)
    if extra:
        tools += "\n\n" + "\n".join(extra)
    lines = init_run.prompt_lines(
        project=project,
        why=why,
        profile_text=kg_profile.render(dict(profile)),
        coverage=coverage,
        strategies=strategies,
        measures=measures,
        settings=settings,
        samples=samples,
        tools=tools,
        prose=init_run.mostly_prose(profile),
    )
    return "\n".join(lines)


def summary(project: str, row_state: str, body: Mapping | None) -> str:
    """One line on a run for a list and a notification."""
    body = body or {}
    decision = body.get("decision") or {}
    if row_state in ("queued", "running"):
        return f"KG init of {project} is {row_state}"
    if row_state == "failed":
        return f"KG init of {project} failed: {str(body.get('error') or 'see the run')[:200]}"
    parts = [p for p in init_run.PARTS if decision.get(p)]
    return f"KG init of {project} decided {', '.join(parts) or 'to change nothing'}"


def report(project: str, run_id: int, why: list[str], state: str, body: Mapping | None, error: str | None) -> str:
    """The markdown report of a run that ended."""
    body = body or {}
    decision = body.get("decision") or {}
    lines = [f"# KG init #{run_id} of {project}: {state}", "", "Asked because: " + ("; ".join(why) or "-") + ".", ""]
    if error:
        lines += [f"Error: {error[:ERROR_CHARS]}", ""]
    for reason in decision.get("reasons") or []:
        figures = ", ".join(f"{k} {v}" for k, v in (reason.get("figures") or {}).items())
        lines.append(f"- {reason['about']}: {reason['why']} ({figures})")
    applied = body.get("applied") or {}
    for part, what in applied.items():
        lines.append(f"- applied {part}: {what}")
    return "\n".join(lines).rstrip() + "\n"
