"""An evolve run on the worker (``evo_agents.worker.runner.evolve``, ``evolve_loop``, ``evolve_eval``,
``evolve_workspace``), without a hub or a runtime: the loop over a fake hub and fake seams (the agent, the verify and a
task's directory), and the git of the best candidate against a local bare repository.

The checks step 2 of the pareto-curator-evolution plan names on the worker: the tree of the evolution is written
(iterations/, frontier.json, evolution_summary.jsonl, eval_traces/ with the whole output of a failed verify); a cached
baseline arm is not run again and an uncached one runs once and is posted; the proposer's directory and prompt hold
neither a holdout task nor any verify command; a long candidate is not cut; a merge of two front candidates that
changed different files is lca_merge's, and one that changed the same file goes to the agent; the best candidate is
reported and, for a git target, committed on its branch and pushed."""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the worker's hub client needs the worker extra, evo-ak[worker]")

from evo_agents.hub import evolve
from evo_agents.worker import gitops
from evo_agents.worker.hubapi import WorkerHub
from evo_agents.worker.runner import evolve_workspace as workspace
from evo_agents.worker.runner.context import _evolve_key
from evo_agents.worker.runner.evolve import EvolveRun, place_agent, place_files, write_branch
from evo_agents.worker.runner.evolve_eval import Checked, Measured, Session, baseline_at
from evo_agents.worker.runner.evolve_loop import Evolution
from evo_agents.worker.runner.kinds import KINDS

SKILL = "skills/demo/SKILL.md"
SIDE = "skills/demo/notes.md"
VERIFY_PROBE = "sh check-probe.sh # probe-answer-key"
VERIFY_REPLAY = "sh check-replay.sh # replay-answer-key"
HOLDOUT = "holdout-task-marker"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


class FakeHub:
    def __init__(self):
        self.arms: list[dict] = []
        self.candidates: list[dict] = []
        self.next_id = 100

    async def arm(self, body: dict) -> dict:
        self.arms.append(body)
        return {"key": "k", "cached": False}

    async def candidate(self, body: dict) -> dict:
        self.candidates.append(body)
        self.next_id += 1
        return {"candidate_id": self.next_id, "frontier": []}


class FakeSeams:
    """A task's directory holds the candidate's files; the task's verify passes when the file its rule names holds
    its word. The proposer and the agent of a merge are ``propose`` and ``resolve``."""

    def __init__(self, root: Path, rules: dict[str, tuple[str, str]], propose=None, resolve=None):
        self.root = root
        self.rules = rules  # verify command -> (path, word)
        self.propose = propose or (lambda folder, number: None)
        self.resolve = resolve or (lambda folder: None)
        self.laid_out: list[str] = []
        self.prompts: list[tuple[Path, str]] = []
        self.verified: list[str] = []

    async def lay_out(self, task, components, name):
        root = self.root / "evals" / name
        repository = root / "repo"
        repository.mkdir(parents=True)
        workspace.write_components(repository, "git", {k: v for k, v in components.items() if v is not None})
        self.laid_out.append(name)
        return root, repository

    async def agent(self, directory, prompt):
        self.prompts.append((directory, prompt))
        if "/merge/" in prompt:
            number = int(prompt.split(f"{workspace.ITERATIONS}/", 1)[1].split("/", 1)[0])
            self.resolve(directory / workspace.ITERATIONS / str(number) / workspace.MERGE)
        elif "This is iteration" in prompt:
            number = int(prompt.split("This is iteration ", 1)[1].split(".", 1)[0])
            self.propose(directory / workspace.ITERATIONS / str(number) / workspace.CANDIDATE, number)
        return Session(
            cost_usd=0.5, turns=3, tool_errors=["Bash: command not found: frob"], summary="I looked at the task."
        )

    async def verify(self, command, cwd):
        self.verified.append(command)
        path, word = self.rules[command]
        target = cwd / path
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        if word in text:
            return Checked(0, "ok\n")
        return Checked(1, f"stdout: checking {path}\nstderr: expected the word {word}\n")


def inputs(**extra) -> dict:
    tasks = [
        {
            "kind": "probe",
            "id": 1,
            "ref": "probe:1",
            "base_sha": None,
            "prompt": "Do the probe task.",
            "verify": VERIFY_PROBE,
        },
        {
            "kind": "replay",
            "id": 2,
            "ref": "replay:2",
            "base_sha": "a" * 40,
            "prompt": "Redo step two.",
            "verify": [VERIFY_REPLAY],
        },
        {"kind": "replay", "id": 3, "split": "holdout", "base_sha": "b" * 40, "prompt": HOLDOUT, "verify": ["x"]},
    ]
    found = {
        "evolve_id": 7,
        "project": "northwind",
        "target": "git",
        "repo": "alpha",
        "base_sha": "c" * 40,
        "branch": "curator/7-demo",
        "paths": [SKILL],
        "baseline": {SKILL: "# Demo\nrules v1\n", SIDE: "side v1\n"},
        "tasks": tasks,
        "cached": {},
        "budget": {"iterations": 2, "max_candidates": 10, "max_cost_usd": 100.0},
        "runtime": "claude-code",
        "model": "m1",
    }
    found.update(extra)
    return found


def every_text(root: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file())


def test_the_loop_writes_its_tree_reads_a_cached_arm_and_hides_verify_and_holdout(tmp_path):
    given = inputs()
    digest = evolve.baseline_digest(given["baseline"])
    cached_key = evolve.arm_key("replay", 2, "a" * 40, digest, "claude-code", "m1")
    given["cached"] = {cached_key: {"score": 1.0, "cost_usd": 0.2, "trace": {"score": 1.0}}}
    long_text = "# Demo\nrules v2 GOOD\n" + "x" * 200_000

    def propose(folder: Path, number: int) -> None:
        (folder / SKILL).write_text(long_text, encoding="utf-8")

    seams = FakeSeams(tmp_path, {VERIFY_PROBE: (SKILL, "GOOD"), VERIFY_REPLAY: (SKILL, "rules")}, propose)
    hub = FakeHub()
    tree = workspace.Tree(tmp_path / "evolution")
    evolution = Evolution(given, hub, seams, tree, rng=random.Random(1), merge_every=0)
    best = asyncio.run(evolution.run())

    # the cached arm is read, the other runs once and is posted
    assert [(arm["task_kind"], arm["task_id"]) for arm in hub.arms] == [("probe", 1)]
    assert hub.arms[0]["baseline_digest"] == digest and hub.arms[0]["result"]["score"] == 0.0
    assert [name for name in seams.laid_out if name.startswith("0-")] == ["0-baseline-probe_1"]
    # the holdout task is never measured, its prompt nowhere
    assert not any("holdout" in name or "replay_3" in name for name in seams.laid_out)
    assert "x" not in seams.verified
    # the seed, then a child, posted whole: 200k characters, not cut
    seed, child = hub.candidates[0], hub.candidates[1]
    assert seed["origin"] == "seed" and seed["parents"] == [] and seed["scores"] == {"probe:1": 0.0, "replay:2": 1.0}
    assert child["origin"] == "mutate" and child["parents"] == ["101"]
    assert child["components"][SKILL] == long_text and child["components"][SIDE] == "side v1\n"
    assert child["scores"] == {"probe:1": 1.0, "replay:2": 1.0}
    assert len(hub.candidates) == 2  # the second iteration's proposer wrote the same files: nothing measured again
    assert best is not None and best.id == "102"
    # the tree
    root = tree.root
    assert (root / "iterations" / "1" / "candidate" / SKILL).read_text(encoding="utf-8") == long_text
    assert json.loads((root / "iterations" / "1" / "parent.json").read_text())["parent"] == "101"
    frontier = json.loads((root / workspace.FRONTIER).read_text())["frontier"]
    assert [row["candidate_id"] for row in frontier] == ["102"]
    lines = [json.loads(line) for line in (root / workspace.SUMMARY).read_text().splitlines()]
    assert [line.get("candidate_id") for line in lines[:2]] == ["101", "102"] and "skipped" in lines[2]
    failed = json.loads((root / workspace.TRACES / "101" / "probe_1.json").read_text())
    assert failed["score"] == 0.0 and failed["checks"][0]["exit_code"] == 1
    assert "stdout: checking" in failed["checks"][0]["output"] and "stderr: expected" in failed["checks"][0]["output"]
    assert failed["tool_errors"] == ["Bash: command not found: frob"] and failed["turns"] == 3
    assert failed["cost_usd"] == 0.5 and failed["prompt"] == "Do the probe task."
    # the proposer works in the tree, which holds no verify command and no holdout task, nor does its prompt
    proposer = [(directory, prompt) for directory, prompt in seams.prompts if "This is iteration" in prompt]
    assert proposer and all(directory == root for directory, _ in proposer)
    text = every_text(root) + "\n".join(prompt for _, prompt in proposer)
    for secret in ("probe-answer-key", "replay-answer-key", "check-probe.sh", HOLDOUT):
        assert secret not in text
    # what the hub keeps of a trace: the end of a failed check's output, no command
    kept = json.dumps(seed["traces"])
    assert "stderr: expected" in kept and "answer-key" not in kept


def test_the_budget_of_cost_stops_the_loop(tmp_path):
    given = inputs(budget={"iterations": 5, "max_candidates": 10, "max_cost_usd": 1.0})
    seams = FakeSeams(tmp_path, {VERIFY_PROBE: (SKILL, "GOOD"), VERIFY_REPLAY: (SKILL, "rules")})
    hub = FakeHub()
    evolution = Evolution(given, hub, seams, workspace.Tree(tmp_path / "t"), merge_every=0)
    asyncio.run(evolution.run())
    assert len(hub.candidates) == 1  # the two baseline arms spent the dollar: no proposer ran
    assert not any("This is iteration" in prompt for _, prompt in seams.prompts)


def merge_setup(tmp_path, ours: dict, theirs: dict, resolve=None):
    given = inputs(
        baseline={"a.md": "a v1\n", "b.md": "b v1\n"},
        tasks=[
            {"kind": "probe", "id": 1, "prompt": "one", "verify": "one"},
            {"kind": "probe", "id": 2, "prompt": "two", "verify": "two"},
        ],
    )
    seams = FakeSeams(tmp_path, {"one": ("a.md", "A+"), "two": ("b.md", "B+")}, resolve=resolve)
    hub = FakeHub()
    evolution = Evolution(given, hub, seams, workspace.Tree(tmp_path / "evolution"), merge_every=1)

    async def build():
        await evolution.seed()
        seed = next(iter(evolution.pool))
        one = [Measured("probe:1", 1.0, 0.1, 1, {}), Measured("probe:2", 0.0, 0.1, 1, {})]
        two = [Measured("probe:1", 0.0, 0.1, 1, {}), Measured("probe:2", 1.0, 0.1, 1, {})]
        a = await evolution.post([seed], "mutate", 1, ours, one)
        b = await evolution.post([seed], "mutate", 2, theirs, two)
        merged = await evolution.merge(3)
        return a, b, merged

    a, b, merged = asyncio.run(build())
    return evolution, hub, seams, a, b, merged


def test_a_merge_of_two_front_candidates_that_changed_different_files_takes_both_without_the_agent(tmp_path):
    evolution, hub, seams, a, b, merged = merge_setup(
        tmp_path, {"a.md": "a A+\n", "b.md": "b v1\n"}, {"a.md": "a v1\n", "b.md": "b B+\n"}
    )
    assert merged is True
    last = hub.candidates[-1]
    assert last["origin"] == "merge" and last["parents"] == [a.id, b.id]
    assert last["components"] == {"a.md": "a A+\n", "b.md": "b B+\n"}
    assert last["scores"] == {"probe:1": 1.0, "probe:2": 1.0}
    assert not any("/merge/" in prompt for _, prompt in seams.prompts)  # no agent: lca_merge took both
    assert evolution.best().id == str(hub.next_id)


def test_a_merge_of_two_candidates_that_changed_the_same_file_apart_is_written_by_the_agent(tmp_path):
    def resolve(folder: Path) -> None:
        request = json.loads((folder / "request.json").read_text())
        assert [item["key"] for item in request["conflicts"]] == ["a.md"]
        assert (folder / "ours" / "a.md").read_text() == "a A+\nours\n"
        assert (folder / "theirs" / "a.md").read_text() == "a theirs\n"
        assert (folder / "ancestor" / "a.md").read_text() == "a v1\n"
        (folder / workspace.RESOLVED / "a.md").write_text("a A+ both\n", encoding="utf-8")

    evolution, hub, seams, a, b, merged = merge_setup(
        tmp_path,
        {"a.md": "a A+\nours\n", "b.md": "b v1\n"},
        {"a.md": "a theirs\n", "b.md": "b B+\n"},
        resolve=resolve,
    )
    assert merged is True
    assert any("/merge/" in prompt for _, prompt in seams.prompts)
    assert hub.candidates[-1]["components"] == {"a.md": "a A+ both\n", "b.md": "b B+\n"}


def git(*args: str, cwd: Path | None = None) -> str:
    command = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    done = subprocess.run(command, capture_output=True, text=True, env={**os.environ, **GIT_IDENTITY}, timeout=60)
    assert done.returncode == 0, f"{command}: {done.stderr}"
    return done.stdout.strip()


def test_the_best_candidate_is_committed_on_its_branch_and_pushed(tmp_path):
    origin, checkout = tmp_path / "origin.git", tmp_path / "alpha"
    git("init", "--quiet", "--bare", "-b", "main", str(origin))
    git("clone", "--quiet", str(origin), str(checkout))
    (checkout / "skills" / "demo").mkdir(parents=True)
    (checkout / SKILL).write_text("# Demo\nrules v1\n", encoding="utf-8")
    (checkout / SIDE).write_text("side v1\n", encoding="utf-8")
    (checkout / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    git("add", "-A", cwd=checkout)
    git("commit", "--quiet", "-m", "base", cwd=checkout)
    git("push", "--quiet", "origin", "main", cwd=checkout)
    base = git("rev-parse", "HEAD", cwd=checkout)
    long_text = "# Demo\nrules v2\n" + "y" * 200_000

    async def body():
        baseline = await baseline_at(checkout, base, [SKILL])
        texts = {SKILL: long_text, SIDE: None}  # the side file removed

        async def push(worktree: Path) -> gitops.Pushed:
            return await gitops.push(worktree, "curator/7-demo", protected=("main",), kind="curator")

        pushed = await write_branch(checkout, base, tmp_path / "best", texts, "curator: evolved", push)
        return baseline, pushed

    baseline, pushed = asyncio.run(body())
    assert baseline == {SKILL: "# Demo\nrules v1\n", SIDE: "side v1\n"}  # the skill's side file read with it
    assert pushed is not None and pushed.branch == "curator/7-demo"
    head = git("rev-parse", "refs/heads/curator/7-demo", cwd=origin)
    assert head == pushed.head and git("rev-parse", f"{head}^", cwd=origin) == base
    assert git("show", f"{head}:{SKILL}", cwd=origin) == long_text.strip()
    assert SIDE not in git("ls-tree", "-r", "--name-only", head, cwd=origin).split()
    assert git("rev-parse", "refs/heads/main", cwd=origin) == base  # the default branch untouched
    assert not (tmp_path / "best").exists()  # the worktree removed


def test_a_push_to_the_default_branch_is_refused(tmp_path):
    origin, checkout = tmp_path / "origin.git", tmp_path / "alpha"
    git("init", "--quiet", "--bare", "-b", "main", str(origin))
    git("clone", "--quiet", str(origin), str(checkout))
    (checkout / "AGENTS.md").write_text("v1\n", encoding="utf-8")
    git("add", "-A", cwd=checkout)
    git("commit", "--quiet", "-m", "base", cwd=checkout)
    git("push", "--quiet", "origin", "main", cwd=checkout)
    base = git("rev-parse", "HEAD", cwd=checkout)

    async def push(worktree: Path) -> gitops.Pushed:
        return await gitops.push(worktree, "main", protected=("main",), kind="curator")

    with pytest.raises(gitops.PushRefused):
        asyncio.run(write_branch(checkout, base, tmp_path / "best", {"AGENTS.md": "v2\n"}, "m", push))
    assert git("rev-parse", "refs/heads/main", cwd=origin) == base


def test_a_task_directory_places_a_skill_where_the_runtime_loads_it_and_an_agent_config(tmp_path):
    place_files(tmp_path, tmp_path / "repo", {SKILL: "skill", SIDE: None, "AGENTS.md": "rules"})
    assert (tmp_path / "repo" / SKILL).read_text() == "skill" and (tmp_path / "repo" / "AGENTS.md").exists()
    assert (tmp_path / ".claude" / "skills" / "demo" / "SKILL.md").read_text() == "skill"
    other = tmp_path / "agent"
    place_agent(other, "Builder", {"instructions": "be brief", "skill": "the skill"})
    assert (other / "CLAUDE.md").read_text() == "be brief" and (other / "AGENTS.md").read_text() == "be brief"
    assert (other / ".claude" / "skills" / "Builder" / "SKILL.md").read_text() == "the skill"
    folder = tmp_path / "candidate"
    workspace.write_components(folder, "agent_config", {"instructions": "i", "skill": "s"})
    assert workspace.read_components(folder, "agent_config", ["instructions", "skill"]) == {
        "instructions": "i",
        "skill": "s",
    }


def test_the_hub_client_sends_the_evolve_key_and_the_kind_is_registered():
    calls = []

    async def call(method, path, body=None, *, headers=None, **_):
        calls.append((method, path, body, headers))
        return {}

    api = WorkerHub("https://hub.example.invalid", None, None)  # type: ignore[arg-type]
    api.call = call  # type: ignore[method-assign]

    async def body():
        await api.evolve_inputs(5, "key-1")
        await api.evolve_arm(5, {"a": 1}, "key-1")
        await api.evolve_candidate(5, {"c": 1}, "key-1")
        await api.evolve_report(5, {"state": "done"}, "key-1")

    asyncio.run(body())
    assert [(method, path) for method, path, _, _ in calls] == [
        ("GET", "/v1/worker/runs/5/evolve"),
        ("POST", "/v1/worker/runs/5/evolve/arm"),
        ("POST", "/v1/worker/runs/5/evolve/candidate"),
        ("POST", "/v1/worker/runs/5/evolve/report"),
    ]
    assert all(headers == {evolve.EVOLVE_KEY_HEADER: "key-1"} for *_, headers in calls)
    assert KINDS["evolve"] is EvolveRun
    curator = {"evolve_key": "secret", "evolve": {}}
    spec = {"curator": curator}
    assert _evolve_key(spec, curator) == "secret" and "evolve_key" not in curator


def test_each_candidate_keeps_the_asi_of_each_task_and_the_next_mutation_reads_its_parents(tmp_path):
    given = inputs()
    seams = FakeSeams(tmp_path, {VERIFY_PROBE: (SKILL, "GOOD"), VERIFY_REPLAY: (SKILL, "rules")})
    hub = FakeHub()
    evolution = Evolution(given, hub, seams, workspace.Tree(tmp_path / "evolution"), merge_every=0)
    asyncio.run(evolution.run())

    seed = hub.candidates[0]
    asi = seed["traces"]["probe:1"]
    assert asi["checks"] == [
        {"exit_code": 1, "output_tail": "stdout: checking skills/demo/SKILL.md\nstderr: expected the word GOOD\n"}
    ]
    assert asi["failed_tools"] == ["Bash: command not found: frob"] and asi["summary"] == "I looked at the task."
    assert seed["traces"]["replay:2"]["checks"] == [{"exit_code": 0}]
    # the mutation's prompt holds what its parent showed on the task it failed, and not on the one it passed
    proposer = [prompt for _, prompt in seams.prompts if "This is iteration 1." in prompt]
    assert proposer and "probe:1" in proposer[0] and "stderr: expected the word GOOD" in proposer[0]
    assert "I looked at the task." in proposer[0] and "replay:2" not in proposer[0]
    assert "answer-key" not in proposer[0] and "check-probe.sh" not in proposer[0]


def test_a_task_verify_runs_with_its_paths_into_a_developers_checkout_written_as_where_each_repo_is(
    tmp_path, monkeypatch
):
    """Replay tasks 105 and 115 of evolution #1 (run #8864) failed on every arm in 0.4 s: their verify reads
    ``git -C ~/github/evo-agents``, a checkout of the developer's machine the worker has not."""
    from types import SimpleNamespace

    from evo_agents.worker.runner import evolve as runner

    ran, fetched = [], []

    async def run_untrusted(run, command, cwd, keep):
        ran.append((command, cwd))
        return 0, ""

    async def fetch(name):
        fetched.append(name)

    monkeypatch.setattr(runner.verify, "run_untrusted", run_untrusted)
    sibling = tmp_path / "checkouts" / "evo-agents"
    run = SimpleNamespace(
        project="evo-agents",
        daemon=SimpleNamespace(checkouts={"evo-agents/evo-agents": {"path": str(sibling)}}),
        check=lambda: None,
        loop=SimpleNamespace(time=lambda: 0.0),
        event=lambda *args: None,
        note=lambda *args, **kw: None,
    )
    owner = EvolveRun.__new__(EvolveRun)
    owner.run, owner.inputs, owner.siblings, owner.fetched = run, {"repo": "evo-agents-harness"}, set(), fetch
    seams = runner.RunSeams(owner)
    repository = tmp_path / "evals" / "0-baseline-replay_105" / "evo-agents-harness"
    command = "git -C ~/github/evo-agents show origin/main:x && cd ~/github/evo-agents-harness && ls"
    for _ in range(2):
        asyncio.run(seams.verify(command, repository))
    assert ran[0] == (f"git -C {sibling} show origin/main:x && cd {repository} && ls", repository)
    assert fetched == ["evo-agents"]  # the other repo's checkout is fetched once, before its first verify
