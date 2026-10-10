"""An author run (kind ``author``, ``AuthorRun``, ``evo_agents.hub.author``): an execution plan written from its
owner's request.

Before anything else the worker downloads the skills its claim names (create-exec-plan, as the hub holds it in the
global scope) through their presigned GETs, checks each one's size, SHA-256 and contents, and fails the run when one is
missing or wrong; then makes a worktree of each of its repos it has a checkout of (the project's harness first, which
it needs; the others it leaves out, saying so), detached at the commit origin's default branch has, as a review run
does (``RunDirectory.prepare_detached``), and writes the skills under ``.claude/skills`` of the run's directory, the
agent's working directory, so nobody syncs skills on this machine. The agent works in EVO_RUN_KIND ``author`` on Claude
Code alone, with Claude Code's question tools turned off (``author.QUESTION_TOOLS``), and talks with its owner in the
run's chat: after each turn the worker posts the agent's last message to the chat (POST /v1/worker/runs/{id}/chat) and
reports ``waiting``, keeping the run, its slot and lease, the time it waits not counted toward its timeout, until the
owner's reply comes through the inbox and starts the next turn in the same session (``author.REPLY_PROMPT``). A run the
hub parks (no reply within a day) keeps its session, worktrees and skills for the run that resumes it, as a plan run
does, which goes on in them with the reply (``author.RESUME_PROMPT``). Once the owner ends the chat (the heartbeat's
``finish``) the run ends done with its summary after the agent's turn: nothing is committed or pushed
(``gitops.check_push`` refuses an author run), and the run's GitHub token reads only.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import author, runs, skill_sync, skills
from evo_agents.hub.client import HubError
from evo_agents.worker.hubapi import Backoff, HubProblem, Unreachable
from evo_agents.worker.runner import access, verify
from evo_agents.worker.runner.common import HARD_STOP_TRIES, Parked, ReportRefused, RunFailed, cut, wait_or
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.transitions import MAX_SUMMARY_CHARS

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


class AuthorRun:
    """An author run: the skills its claim names downloaded and checked, a worktree of each of its repos this worker
    has a checkout of, detached at origin's default branch, the skills written for the agent in the run's directory,
    the agent writing a plan from its owner's request and talking with the owner in the run's chat, waiting for the
    owner's reply after each turn (parked when it waits too long, and resumed in the same session and directory), and
    nothing committed or pushed at its end, once the owner ended the chat."""

    parks = True
    chat = True

    def __init__(self, run: Run):
        self.run = run
        run.title = run.spec.get("title") or f"author run of {run.spec.get('project')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(
            run,
            self.directory,
            reply_prompt=author.REPLY_PROMPT,
            resume_prompt=author.RESUME_PROMPT,
            after_turn=self.after_turn,
        )
        self.left_out: list[str] = []  # repos of the run this worker has no checkout of
        self.last_said: str | None = None  # the agent's last message in the chat

    async def steps(self) -> None:
        try:
            await self._work()
        except Parked:
            await self.conversation.park()

    async def _work(self) -> None:
        run, spec = self.run, self.run.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        names = ", ".join(str(entry.get("repo")) for entry in repos) or "no repo"
        resume_of = self.conversation.resume_of
        resumes = f", going on from parked run #{resume_of}" if resume_of else ""
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: an author run of project {run.project} over "
            f"{names}, {run.runtime}, timeout {run.timeout_s // 60} min of agent time{resumes}. It reads only: "
            "nothing is committed or pushed."
        )
        if run.runtime not in author.AUTHOR_RUNTIMES:
            raise RunFailed(f"an author run runs on {', '.join(author.AUTHOR_RUNTIMES)} only, not {run.runtime}")
        cls = run.agent.runtime_class()
        if run.mode == "interactive":
            raise RunFailed("an author run runs headless only")
        if not repos:
            raise RunFailed("the author run names no repo, not even the project's harness")
        if resume_of is not None:  # the parked run's directory, worktrees and skills, when they are still here
            self.conversation.adopt()
        bundles = await self.fetch_skills() if self.directory.path is None else []
        held = self.held_repos(repos)
        names = [entry.get("repo") for entry in held]
        await access.take_credentials(run, names)
        run.check()
        await access.check(run, names, path=access.run_path(run))
        run.check()
        await self.prepare(held, bundles)
        prompt, session_id = await self.conversation.first_turn(self.prompt())
        await self.conversation.turns(cls, prompt, session_id)
        run.summary = verify.read_summary(run, self.directory.path) or (
            cut(self.last_said, MAX_SUMMARY_CHARS) if self.last_said else None
        )
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        await run.reports.end("done", summary=run.summary, usage=run.outcome.usage if run.outcome else None)

    async def prepare(self, held: list[dict], bundles: list[tuple[dict, skills.Contents]]) -> None:
        """The run's directory: new read-only worktrees of the repos ``held`` with the skills written in it, or the
        parked run's, adopted."""
        run, directory = self.run, self.directory
        if directory.path is None:
            await directory.prepare_detached(held)
            run.check()
            self.write_skills(bundles)
        else:
            run.worktree = directory.path
            (directory.path / runs.RESULT_DIR).mkdir(mode=0o700, exist_ok=True)
            directory.save()

    # The chat

    async def post_chat(self) -> None:
        """Post the agent's last message of the turn to the run's chat, trying a few times; a message the hub does
        not take is noted, and the run goes on."""
        run = self.run
        text = author.chat_text(run.outcome.summary if run.outcome else None)
        if text is None:
            run.note("The agent's turn ended without a message for the chat.")
            return
        self.last_said = text
        backoff = Backoff()
        for _ in range(HARD_STOP_TRIES + 1):
            try:
                await run.daemon.hub.chat(run.id, text)
                return
            except Unreachable:
                await wait_or(run.daemon.hard_stop, backoff.next())
            except HubProblem as exc:
                run.note(f"The hub did not take the agent's message for the chat: {exc}")
                return
        run.note("The hub did not answer: the agent's message did not reach the chat.")

    async def after_turn(self) -> list[dict]:
        """After each turn of the agent: its last message to the chat, then what starts its next turn: the owner's
        messages it has not had, at once; else, unless the owner ended the chat, the reply the run waits for. None
        when the run ends."""
        run = self.run
        await self.post_chat()
        unread = await run.inbox.unread()
        if unread:
            run.note(f"{len(unread)} message(s) of the owner came while the agent worked: a new turn takes them.")
            return unread
        if run.finish_asked.is_set():
            run.note("The agent's turn ended, and the owner ended the chat: the run ends done.")
            return []
        await self.conversation.ensure_running()
        try:
            await run.reports.report("waiting", only_from=("running",))
        except ReportRefused as exc:
            if exc.status != 409:
                raise
            run.note(f"The agent's turn ended, and the hub takes no wait for a reply ({exc}): the run ends done.")
            return []
        if run.state != "waiting":
            return []
        run.note(
            "The agent's turn ended: the run waits for its owner's reply in the chat, and the time it waits does not "
            "count toward its timeout."
        )
        messages = await self.conversation.wait_for_answer()
        if not messages:
            run.note("The owner ended the chat: the run ends done.")
        return messages

    # The repos and the skills

    def held_repos(self, repos: list[dict]) -> list[dict]:
        """The repos of the run this worker has a checkout of; RunFailed without one of the first, the harness."""
        run = self.run
        harness = repos[0].get("repo")
        if not isinstance(harness, str) or run.daemon.checkout_for(run.project, harness) is None:
            raise RunFailed(
                f"this worker has no checkout of {run.project}/{harness}, the project's harness, which an author run "
                "reads the project's plans in: clone it where the harness registry places it, then dispatch again"
            )
        held = []
        for entry in repos:
            name = entry.get("repo")
            if isinstance(name, str) and run.daemon.checkout_for(run.project, name) is not None:
                held.append(entry)
            else:
                self.left_out.append(str(name))
        if self.left_out:
            run.note(f"No checkout of {', '.join(self.left_out)} on this worker: the agent works without them.")
        return held

    async def fetch_skills(self) -> list[tuple[dict, skills.Contents]]:
        """Each skill the claim names, downloaded through its presigned GET and checked (size, SHA-256, contents) as
        ``hub skills sync`` checks it; RunFailed when one is wrong or author.AUTHOR_SKILL is not among them."""
        wanted = author.AUTHOR_SKILL
        items = [item for item in self.run.spec.get("skills") or [] if isinstance(item, dict)]
        if not any(str(item.get("name") or "").lower() == wanted.lower() for item in items):
            raise RunFailed(
                f"the claim handed this author run no skill {wanted}: the hub holds none in the global scope, or is "
                "older than this worker; publish it with `evo-agents hub skills publish`, then dispatch again"
            )
        found = [(item, await self.fetch_skill(item)) for item in items]
        self.run.note(
            "Skills from the hub for the agent: "
            + ", ".join(f"{item.get('name')} version {item.get('version')}" for item, _ in found)
            + "."
        )
        return found

    async def fetch_skill(self, item: dict) -> skills.Contents:
        """The skill ``item`` of the claim names, downloaded and checked; RunFailed when it is wrong."""
        name, version = str(item.get("name") or ""), item.get("version")
        problem = skills.name_problem(name)
        if problem:
            raise RunFailed(f"the claim names a skill this worker cannot write: {problem}")
        try:
            size = int(item["size"])
            data = await asyncio.to_thread(skill_sync.get_bundle, str(item["url"]), size)
        except (KeyError, TypeError, ValueError):
            raise RunFailed(f"the claim names skill {name} without its bundle's URL and size") from None
        except HubError as exc:
            raise RunFailed(f"the bundle of skill {name} (version {version}) did not download: {exc}") from None
        if len(data) != size or hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise RunFailed(
                f"the bundle of skill {name} (version {version}) is not the one the hub recorded: another size or "
                "SHA-256; nothing was written"
            )
        try:
            return skills.read_bundle(data, name)
        except skills.BundleError as exc:
            raise RunFailed(f"the bundle of skill {name} (version {version}) is not a skill: {exc}") from None

    def write_skills(self, bundles: list[tuple[dict, skills.Contents]]) -> None:
        """Write each skill under author.SKILLS_DIR of the run's directory, where the agent's runtime finds it."""
        directory = self.directory.path
        base = directory.joinpath(*author.SKILLS_DIR.split("/"))
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            for item, contents in bundles:
                target = base / str(item["name"])
                target.mkdir(mode=0o700)
                skills.write_tree(contents, target)
        except (OSError, skills.BundleError) as exc:
            raise RunFailed(f"cannot write the skills under {base}: {exc}") from None
        written = ", ".join(f"{author.SKILLS_DIR}/{item['name']}" for item, _ in bundles)
        self.run.note(f"Wrote {written} in {directory}.")

    def prompt(self) -> str:
        """The run's prompt, with where each repo's worktree is when a folder is not named as its repo, and the repos
        this worker has no checkout of."""
        prompt = self.run.spec.get("prompt") or ""
        moved = self.directory.moved()
        extra = []
        if moved:
            extra += ["", "Worktree folders that are not named as their repo:", *moved]
        if self.left_out:
            extra += ["", f"Repos of the project this worker has no checkout of: {', '.join(self.left_out)}."]
        return runs.clip(prompt + "\n".join(extra) + ("\n" if extra else ""), runs.MAX_PROMPT_BYTES)

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        """An author run changes nothing: no diff is uploaded."""
        return b""

    async def release_branch(self) -> None:
        await self.directory.release()
