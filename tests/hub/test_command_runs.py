"""The hub's side of a command run (plan hub-cron-jobs, criterion a5; ``evo_agents.hub.server.command_dispatch``): the
run of a cron job's shell command line, queued for its owner, which only a daemon that says it runs command runs
claims, with the command and no prompt; a worker set to take runs dispatched from the web alone passes over one a
hub queued; the run is leased its owner's secrets and the App's token for its repo but no model account; the
checks of runs hold its timeout and its command."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from evo_agents.hub import command_runs, runs, tables
from evo_agents.hub.server import command_dispatch
from evo_agents.hub.server.runs.service.dispatch import insert_run
from tests.hub import live
from tests.hub.live import sql
from tests.hub.test_credentials_api import (
    MINE,
    OWNER,
    PROJECT,
    account_pool,
    app_key,  # noqa: F401 (a fixture)
    by_name,
    client,  # noqa: F401 (a fixture)
    config,  # noqa: F401 (a fixture)
    env_secret,
    hub,  # noqa: F401 (a fixture)
    install,
    leased,
    pooled,
    sample,
    worker_of,
)
from tests.hub.test_runs import beat, claim, moved, of_run, report

OLD_KINDS = [kind for kind in runs.RUN_KINDS if kind != "command"]  # a daemon from before command runs


def ids(client) -> tuple[int, int]:
    """(project id, owner's user id)."""
    db = live.database_of(client)
    project = sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    owner = sql(db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0][0]
    return project, owner


def queued(client, command: str = "make nightly-report", repo: str | None = "evo-agents", **extra) -> int:
    """A command run of ``command`` in ``repo`` for the owner, as a cron job queues one."""
    project_id, owner_id = ids(client)

    async def made() -> int:
        async with client.app.state.engine.begin() as conn:
            return await command_dispatch.queue_command_run(
                conn, project_id=project_id, owner_id=owner_id, command=command, repo=repo, **extra
            )

    return client.portal.call(made)


def workers_for(client, via: str = "machine") -> list[str]:
    project_id, owner_id = ids(client)

    async def found() -> list[str]:
        async with client.app.state.engine.connect() as conn:
            return await command_dispatch.command_workers(conn, project_id, owner_id, via)

    return client.portal.call(found)


def kinds_worker(client, headers, name: str, kinds) -> dict:
    worker = worker_of(client, headers, name)
    beat(client, worker, run_kinds=list(kinds))
    return worker


def test_a_command_run_goes_only_to_a_daemon_that_runs_command_runs_with_its_command_and_no_prompt(client, hub):
    old = kinds_worker(client, hub["owner"], "old", OLD_KINDS)
    new = kinds_worker(client, hub["owner"], "new", runs.RUN_KINDS)
    run_id = queued(client, "make nightly-report && echo ok")
    assert claim(client, old) is None, "a daemon that does not say it runs command runs never claims one"
    spec = claim(client, new)
    assert (spec["id"], spec["kind"], spec["command"], spec["prompt"]) == (
        run_id,
        "command",
        "make nightly-report && echo ok",
        "",
    )
    assert [entry["repo"] for entry in spec["repos"]] == ["evo-agents"]
    assert spec["runtime"] in runs.RUNTIMES, "of record only: a leased run names a runtime"
    assert (spec["timeout_min"], spec["max_attempts"]) == (30, 1)
    assert not spec.get("account_pool"), "no account pool gives a command run a model account"
    moved(client, new, run_id, "running")
    answer = report(client, new, run_id, "failed", error="The command exited 2", failure_cause="command_failed")
    assert answer.status_code == 200, answer.text
    assert of_run(live.database_of(client), run_id, "state", "failure_cause") == [("failed", "command_failed")]

    other = queued(client, "date", repo=None)
    spec = claim(client, new)
    assert (spec["id"], spec["repos"], spec["command"]) == (other, None, "date")
    plain = claim(client, old)
    assert plain is None


def test_a_worker_of_dispatch_from_web_takes_no_command_run_the_hub_queued(client, hub):
    web = kinds_worker(client, hub["owner"], "web-only", runs.RUN_KINDS)
    w = tables.workers
    sql(live.database_of(client), update(w).values(dispatch_from="web").where(w.c.id == web["id"]))
    queued(client)
    assert workers_for(client) == [] and workers_for(client, "web") == ["web-only"]
    assert claim(client, web) is None
    kinds_worker(client, hub["owner"], "old", OLD_KINDS)
    assert workers_for(client) == [], "a daemon that does not say it runs command runs is none of them"
    anywhere = kinds_worker(client, hub["owner"], "anywhere", runs.RUN_KINDS)
    assert workers_for(client) == ["anywhere"]
    assert claim(client, anywhere)["kind"] == "command"


def test_a_worker_in_no_pool_of_the_default_agent_is_none_of_the_command_workers(client, hub):
    node = kinds_worker(client, hub["owner"], "node", runs.RUN_KINDS)
    loose = kinds_worker(client, hub["owner"], "loose", runs.RUN_KINDS)
    w = tables.workers
    sql(live.database_of(client), update(w).values(pool_id=None).where(w.c.id == loose["id"]))
    assert workers_for(client) == ["node"], "the claim gives a run of the default agent to its pool's nodes alone"
    run_id = queued(client)
    assert claim(client, loose) is None
    assert claim(client, node)["id"] == run_id


def test_a_command_run_is_leased_its_owners_secrets_and_repo_token_but_no_model_account(client, hub, github):
    install(github, "evo-agents")
    env_secret(client, hub["owner"], "deploy-token", "DEPLOY_TOKEN", sample("deploy-"))
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample("oauth-"))
    account_pool(client, "claude-oauth")
    worker = kinds_worker(client, hub["owner"], "mac-mini", runs.RUN_KINDS)
    run_id = queued(client)
    assert claim(client, worker)["id"] == run_id
    leases = by_name(leased(client, worker, run_id))
    assert set(leases) == {"deploy-token", f"github-app:{MINE}"}
    assert "claude-oauth" not in leases and pooled("claude-oauth") not in leases


def test_the_checks_of_runs_hold_a_command_runs_timeout_and_command(client, hub):
    with pytest.raises(ValueError, match="240"):
        queued(client, timeout_min=command_runs.MAX_TIMEOUT_MINUTES + 1)
    with pytest.raises(ValueError, match="empty"):
        queued(client, " ")
    run_id = queued(client, timeout_min=command_runs.MAX_TIMEOUT_MINUTES)
    assert of_run(live.database_of(client), run_id, "timeout_s", "request", "plan_id") == [
        (14400, "make nightly-report", None)
    ]
    project_id, owner_id = ids(client)
    values = {
        "kind": "command",
        "project_id": project_id,
        "title": "cron",
        "dispatched_by": owner_id,
        "requested_runtime": "any",
        "runtime": "any",
        "mode": "headless",
        "approval": "auto",
        "timeout_s": 14400,
        "request": "date",
    }

    def insert(changed: dict) -> int:
        async def made() -> int:
            async with client.app.state.engine.begin() as conn:
                return await insert_run(conn, {**values, **changed})

        return client.portal.call(made)

    assert insert({}) > run_id
    with pytest.raises(IntegrityError, match="runs_command_timeout_check"):
        insert({"timeout_s": 14460})
    with pytest.raises(IntegrityError, match="runs_author_request_check"):
        insert({"request": None})
