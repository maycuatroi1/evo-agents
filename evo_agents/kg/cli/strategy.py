"""``evo-agents kg strategy``: list the KG strategies, show one, or apply one to the project (``--dry-run`` says what
it would change), as ``evo_agents.kg.strategy`` reads them (docs/kg-strategies.md)."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.kg.cli.base import WITH_PROJECT, Failed, KgCommand, KgContext, shown


def yaml_dump(data) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip()


class KgStrategy(KgCommand):
    name = "strategy"
    help = "list, show or apply a KG strategy for a kind of data (docs/kg-strategies.md)"
    arguments = (
        *WITH_PROJECT,
        Arg("action", choices=["list", "show", "apply"]),
        Arg("target", nargs="?", help="a strategy name, or a strategy file (apply, show)"),
        Arg("--dry-run", action="store_true", help="apply: say what would change and change nothing"),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg import strategy as kg_strategy

        args = ctx.args
        project = self.project_of(ctx)
        root = project.harness.root if project is not None else None
        try:
            if args.action == "list":
                found = kg_strategy.available(root)
                return shown(ctx, [s.to_json() for s in found.values()], Text(kg_strategy.render_list(found)))
            if not args.target:
                raise kg_strategy.StrategyError(f"{args.action} needs a strategy name or file")
            chosen = kg_strategy.find(args.target, root)
            if args.action == "show":
                text = Text(f"{chosen.name} ({chosen.origin}): {chosen.description}", yaml_dump(chosen.to_json()))
                return shown(ctx, chosen.to_json(), text)
            found = kg_strategy.plan(chosen, project) if args.dry_run else kg_strategy.apply(chosen, project)
        except kg_strategy.StrategyError as exc:
            raise Failed(str(exc), 2) from exc
        out = {**found.to_json(), "applied": not args.dry_run}
        lines = [f"cannot apply: {problem}" for problem in found.problems]
        verb = "would change" if args.dry_run else "changed"
        lines += [f"{verb} {key}: {change['from']} -> {change['to']}" for key, change in found.changed.items()]
        if out["adds_identifiers"] or out["adds_ontology"]:
            lines.append(f"{verb} knowledge: identifiers {out['adds_identifiers']}, ontology {out['adds_ontology']}")
        if found.needs_rebuild:
            lines.append(f"takes effect with the next build: {', '.join(found.needs_rebuild)}")
        return shown(ctx, out, Text(*lines), 1 if found.problems else 0)

    def project_of(self, ctx: KgContext):
        """The project: the one named, which apply needs; for list and show, which work anywhere, the project of the cwd
        when there is one, which adds its own strategies."""
        if ctx.args.action == "apply" or ctx.args.project:
            return ctx.project
        from evo_agents.kg.project import ProjectError, resolve_project

        try:
            return resolve_project(None)
        except ProjectError:
            return None


STRATEGY = KgStrategy()
