"""``evo-agents kg eval-questions``: print a question set for ``kg eval`` (JSONL), from the done steps of the hub's
plans (``evo_agents.hub.kg_eval``) or, with ``--from-sessions``, from how agents searched the code in the Claude Code
transcripts of the harness (``evo_agents.hub.kg_eval_sessions``). What it read and left out goes to stderr."""

from __future__ import annotations

import json
import time
from pathlib import Path

from evo_agents.clikit import Arg, Result, Stderr, Text, Views
from evo_agents.kg import evaluate as ev
from evo_agents.kg.cli.base import Failed, KgCommand, KgContext, bounded, project_failed, repo_overrides


def jsonl(questions) -> Text:
    """One question a line."""
    return Text(*(json.dumps(question, ensure_ascii=False) for question in questions))


class KgEvalQuestions(KgCommand):
    name = "eval-questions"
    help = (
        "print a question set for `kg eval` from the done steps of the hub's plans, or from how agents searched"
        " the code in this harness's sessions (JSONL)"
    )
    arguments = (
        Arg("--project", help="hub project (default: the project of the current directory)"),
        Arg("--plan", action="append", help="only this plan (repeatable)"),
        Arg("--text", choices=ev.PLAN_TEXTS, default="title", help="what of a step asks (default: title)"),
        Arg(
            "--max-files",
            type=bounded(1, 1000),
            help=f"leave out a step that changes more files (default {ev.DEFAULT_MAX_FILES})",
        ),
        Arg("--repo", action="append", metavar="NAME=PATH", help="checkout of repo NAME (default: the harness's)"),
        Arg(
            "--from-sessions",
            action="store_true",
            help="questions from the code searches in the Claude Code transcripts of the harness, not from the plans",
        ),
        Arg(
            "--days",
            type=bounded(1, 3650),
            default=ev.DEFAULT_SESSION_DAYS,
            help=f"with --from-sessions: tool calls of the last N days (default {ev.DEFAULT_SESSION_DAYS})",
        ),
        Arg(
            "--transcripts",
            action="append",
            metavar="DIR",
            help="with --from-sessions: a Claude Code projects directory to read (repeatable; default: this machine's)",
        ),
    )

    def run(self, ctx: KgContext) -> Result:
        if ctx.args.from_sessions:
            return self.from_sessions(ctx)
        from evo_agents.hub.client import Hub, HubError, load_credentials
        from evo_agents.hub.kg_eval import plan_questions
        from evo_agents.kg.project import ProjectError, resolve_project

        args = ctx.args
        try:
            overrides = repo_overrides(args.repo)
        except ValueError as exc:
            raise Failed(str(exc), 2) from exc
        try:
            name = resolve_project(args.project).name
        except ValueError as exc:
            raise Failed(str(exc), 2) from exc
        except ProjectError as exc:
            if not args.project or "/" in args.project:
                raise project_failed(exc) from exc
            name = args.project  # a hub project with no harness on this machine: give its checkouts with --repo
        try:
            credentials = load_credentials()
            questions, skipped = plan_questions(
                Hub(credentials.url, credentials.token),
                name,
                ev.project_checkouts(args.project, overrides),
                plans=args.plan or (),
                text=args.text,
                max_files=args.max_files or ev.DEFAULT_MAX_FILES,
            )
        except HubError as exc:
            raise Failed(str(exc)) from exc
        said = [f"{len(questions)} question(s) from the plans of {name}; {len(skipped)} step(s) left out"]
        said += [f"  left out {why}" for why in skipped]
        return Result(None, Views(jsonl(questions), Stderr(Text(*said))))

    def from_sessions(self, ctx: KgContext) -> Result:
        from evo_agents.hub.kg_eval_sessions import session_questions
        from evo_agents.hub.transcripts import claude_roots
        from evo_agents.kg.project import ProjectError, resolve_project

        args = ctx.args
        try:
            if args.plan:
                raise ValueError("--plan picks plans, which --from-sessions does not read")
            overrides = repo_overrides(args.repo)
            project = resolve_project(args.project)
        except (ValueError, ProjectError) as exc:
            raise Failed(str(exc), 2) from exc
        roots = [Path(path).expanduser() for path in args.transcripts] if args.transcripts else claude_roots()
        found = session_questions(project, roots, since=time.time() - args.days * 86400, checkouts=overrides)
        said = [
            f"{len(found.questions)} question(s) from {found.transcripts} transcript(s) of {project.name} in the last"
            f" {args.days} day(s); of {found.searches} search(es) of its repos, {found.no_identifier} named no"
            f" identifier and {found.unanswered} opened no file of the repo after"
        ]
        said += [f"  {repo}: {count}" for repo, count in found.per_repo()]
        return Result(None, Views(jsonl(found.questions), Stderr(Text(*said))))


EVAL_QUESTIONS = KgEvalQuestions()
