"""A run of a repo the worker has no checkout of (``evo_agents.worker.runner.clones``), with the daemon, the fake hub
and the two bare origins of ``tests.worker.test_plan_runs``: the daemon clones the repo from the origin the hub's
answer to the leases names, in the project's workspace, keeps it as its checkout (in its heartbeat and config.json),
and the run goes on in it; a work tree already where the repo goes is taken as it is, nothing cloned; a repo nothing
names an origin for fails the run with cause origin before the agent starts."""

from __future__ import annotations

import asyncio
import shutil
import signal

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import runs
from evo_agents.worker.daemon import Daemon
from evo_agents.worker.home import WorkerConfig
from tests.worker import fake_adapter
from tests.worker.fake_hub import TOKEN, FakeHub
from tests.worker.test_plan_runs import PLAN, PROJECT, Machine, git, machine, plan_body, run_repos  # noqa: F401


def run_with(machine: Machine, sources: list[dict], *, held: tuple[str, ...]) -> dict:
    """Queue a plan run of alpha and beta on a daemon that holds a checkout of ``held`` alone, its project's workspace
    the one the machine's checkouts are in, the hub naming ``sources`` as where the run's repos come from; the run's
    end, its notes, and what the daemon and config.json then hold."""
    found: dict = {}

    async def go():
        hub = FakeHub(PROJECT)
        hub.put_plan(plan_body())
        url = await hub.start()
        workspace = machine.checkouts["alpha"].parent
        config = WorkerConfig(
            url=url,
            worker_id=1,
            name="mac",
            projects=[PROJECT],
            repos={PROJECT: {"workspace": str(workspace), "repos": [], "harness": None}},
            checkouts={f"{PROJECT}/{name}": str(machine.checkouts[name]) for name in held},
        )
        machine.home.save(config, TOKEN)
        daemon = Daemon(
            machine.home, config, TOKEN, adapters={"claude-code": fake_adapter.FakeAdapter}, env=machine.env
        )
        task = asyncio.create_task(daemon.run())
        try:
            for _ in range(600):
                if getattr(daemon, "beat_ok", None) is not None and daemon.beat_ok.is_set():
                    break
                await asyncio.sleep(0.05)
            assert runs.CLONES_CHECKOUTS in hub.last_heartbeat["features"]
            run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
            hub.leases[run_id] = {"leases": [], "missing": [], "repos": sources}
            found["state"] = await hub.wait_state(run_id, "done", "failed")
            found["run"] = hub.runs[run_id]
            found["texts"] = hub.texts(run_id)
            found["checkouts"] = dict(daemon.checkouts)
        finally:
            daemon._signal(signal.SIGTERM)
            done, _ = await asyncio.wait({task}, timeout=30)
            if not done:
                task.cancel()
            await hub.stop()

    asyncio.run(go())
    found["config"] = machine.home.load_config()
    return found


def test_a_repo_without_a_checkout_is_cloned_from_the_hubs_origin_and_the_run_goes_on_in_it(machine):
    shutil.rmtree(machine.checkouts["beta"])
    machine.scenarios({f"plan:{PLAN}": [{"write": {"beta/b.txt": "b\n"}}, {"result": {"summary": "wrote b"}}]})
    sources = [
        {"repo": "alpha", "origin": str(machine.origins["alpha"]), "path": "alpha", "project": PROJECT},
        {"repo": "beta", "origin": str(machine.origins["beta"]), "path": "beta", "project": PROJECT},
    ]
    found = run_with(machine, sources, held=("alpha",))
    assert found["state"] == "done", (found["run"], found["texts"])
    target = machine.checkouts["beta"]
    assert git("rev-parse", "HEAD", cwd=target) == machine.seeds["beta"], "cloned where the project's repos go"
    assert any(text.startswith(f"Cloning beta from {machine.origins['beta']} into {target}") for text in found["texts"])
    assert found["checkouts"][f"{PROJECT}/beta"]["path"] == str(target)
    assert found["config"].checkouts[f"{PROJECT}/beta"] == str(target), "config.json keeps it for the next start"
    assert "b.txt" in machine.origin("beta", "ls-tree", "-r", "--name-only", "feat/beta").split()
    assert not list(target.parent.glob(".beta.clone-*")), "no partial clone is left beside it"


def test_a_work_tree_where_the_repo_goes_is_taken_as_it_is(machine):
    machine.scenarios({f"plan:{PLAN}": [{"result": {"summary": "nothing to do"}}]})
    head = git("rev-parse", "HEAD", cwd=machine.checkouts["beta"])
    sources = [{"repo": "beta", "origin": None, "path": "beta", "project": PROJECT}]
    found = run_with(machine, sources, held=("alpha",))
    assert found["state"] == "done", (found["run"], found["texts"])
    assert f"Found a checkout of beta at {machine.checkouts['beta']}: the run uses it." in found["texts"]
    assert not any(text.startswith("Cloning beta") for text in found["texts"])
    assert git("rev-parse", "HEAD", cwd=machine.checkouts["beta"]) == head


def test_a_repo_nothing_names_an_origin_for_fails_the_run_with_cause_origin(machine):
    shutil.rmtree(machine.checkouts["beta"])
    machine.scenarios({f"plan:{PLAN}": [{"result": {"summary": "never"}}]})
    found = run_with(machine, [], held=("alpha",))
    assert found["state"] == "failed"
    assert found["run"]["failure_cause"] == "origin"
    assert "no checkout of demo/beta, and neither project demo nor another project" in found["run"]["error"]
    assert not machine.checkouts["beta"].exists()
