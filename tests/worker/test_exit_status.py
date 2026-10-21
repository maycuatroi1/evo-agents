"""The exit status of the worker's commands and of its daemon (``evo_agents.worker.cli``, docs/workers.md): a command
exits 69 when the hub or the network could not be reached, 75 when the hub answered 5xx or 429 and 1 for another
refusal, as every evo-agents command does (``evo_agents.hub.client.exit_status``); the daemon keeps its own: 3 (or
EVO_WORKER_REVOKED_EXIT) once its token is revoked, 1 when the hub answers 426. A hub on 127.0.0.1 answers every
request with the status the test sets; no Postgres."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from aiohttp import web

from evo_agents.cli import main
from evo_agents.hub.client import EXIT_FAILED, EXIT_TEMPFAIL, EXIT_UNAVAILABLE, HubError, Unreachable, exit_status
from evo_agents.worker import hubapi
from evo_agents.worker.daemon import Daemon
from evo_agents.worker.home import EXIT_REVOKED, REVOKED_EXIT_VARIABLE, WorkerConfig
from tests.hub import pg
from tests.worker import fake_adapter
from tests.worker.fake_hub import TOKEN
from tests.worker.test_plan_runs import PROJECT, machine  # noqa: F401 (machine, a fixture)


async def answering(status: int) -> tuple[web.AppRunner, str]:
    """A hub that answers every request with ``status``."""

    async def answer(request: web.Request) -> web.Response:
        return web.json_response({"error": "test", "message": f"the test hub answers {status}"}, status=status)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", answer)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def daemon_exit(machine, status: int, env: dict | None = None) -> int:  # noqa: F811
    async def go() -> int:
        runner, url = await answering(status)
        config = WorkerConfig(url=url, worker_id=1, name="mac", projects=[PROJECT], checkouts={})
        machine.home.save(config, TOKEN)
        daemon = Daemon(
            machine.home,
            config,
            TOKEN,
            adapters={"claude-code": fake_adapter.FakeAdapter},
            env={**machine.env, **(env or {})},
        )
        try:
            return await asyncio.wait_for(daemon.run(), timeout=60)
        finally:
            await runner.cleanup()

    return asyncio.run(go())


def test_the_daemon_exits_1_when_the_hub_answers_426_and_3_when_its_token_is_revoked(machine):  # noqa: F811
    assert daemon_exit(machine, 426) == EXIT_FAILED, "a protocol the hub no longer speaks: upgrade, then start again"
    assert daemon_exit(machine, 401) == EXIT_REVOKED == 3
    assert daemon_exit(machine, 403) == EXIT_REVOKED
    assert daemon_exit(machine, 401, {REVOKED_EXIT_VARIABLE: "0"}) == 0  # the LaunchAgent's


@pytest.mark.parametrize(
    "problem, status",
    [
        (hubapi.Unreachable("cannot reach http://127.0.0.1:9: ClientConnectorError"), EXIT_UNAVAILABLE),
        (hubapi.Unreachable("the hub answered HTTP 503", 503), EXIT_TEMPFAIL),
        (hubapi.Unreachable("too many requests", 429), EXIT_TEMPFAIL),
        (hubapi.Refused("no such run", 404, "not_found"), EXIT_FAILED),
        (hubapi.Outdated("upgrade evo-agents", 426), EXIT_FAILED),
    ],
)
def test_exit_status_of_a_worker_command_follows_the_problem_of_its_call(problem, status):
    found = problem.hub_error()
    assert isinstance(found, HubError) and str(found) == str(problem) and exit_status(found) == status
    assert isinstance(found, Unreachable) == (status == EXIT_UNAVAILABLE)


def test_exit_status_of_worker_join_says_why_it_failed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVO_WORKER_HOME", str(tmp_path / "worker"))
    monkeypatch.setenv("HOME", str(tmp_path))

    async def join(status: int) -> int:
        runner, url = await answering(status)
        try:
            return await asyncio.to_thread(main, ["worker", "join", "--url", url, "--code", "ABCD-EFGH"])
        finally:
            await runner.cleanup()

    for answered, status in ((503, EXIT_TEMPFAIL), (429, EXIT_TEMPFAIL), (400, EXIT_FAILED)):
        assert asyncio.run(join(answered)) == status, answered
        assert capsys.readouterr().err.startswith("error: ")
    nobody = f"http://127.0.0.1:{pg.free_port()}"
    assert main(["worker", "join", "--url", nobody, "--code", "ABCD-EFGH"]) == EXIT_UNAVAILABLE
    assert "cannot reach" in capsys.readouterr().err
