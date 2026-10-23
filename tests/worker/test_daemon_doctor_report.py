"""The daemon sends its doctor report when it starts (plan credential-sharing, criterion a6): ``evo-agents worker
run`` against a real hub (``tests.worker.test_daemon.make_stack``), whose list of workers then shows when the report
came and what it found; nothing the hub keeps of it, and nothing the daemon logs, holds a token."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from sqlalchemy import select  # noqa: E402

from evo_agents.hub import tables  # noqa: E402
from tests.hub.live import sql  # noqa: E402
from tests.worker.test_daemon import TOKEN_LEAK, finished_cleanly, make_stack, wait_until  # noqa: E402, F401


def test_doctor_report_the_daemon_sends_one_at_start_without_a_token(make_stack, hub_db):
    stack = make_stack()
    config = stack.register()
    proc = stack.start_daemon()

    def reported():
        rows = stack.client.get("/v1/org/workers", headers=stack.owner).json()
        return next((row for row in rows if row["doctor_at"] is not None), None)

    row = wait_until(reported, "the daemon's doctor report", explain=stack.daemon_output)
    assert row["worker"] == config["name"] and row["doctor_high"] >= 0 and row["doctor_medium"] >= 0
    stack.wait_log("doctor report sent", proc)
    w = tables.workers
    ((findings,),) = sql(hub_db, select(w.c.doctor_findings).where(w.c.id == row["id"]))
    assert all(set(finding) == {"code", "severity", "detail"} for finding in findings)
    kept = repr(findings)
    assert not TOKEN_LEAK.search(kept) and stack.worker_token not in kept and stack.machine_token not in kept
    finished_cleanly(stack, proc)
