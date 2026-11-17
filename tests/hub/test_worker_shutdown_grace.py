"""The grace a hub worker gives its running jobs after SIGTERM: worker-kg waits for the build a deploy would otherwise
throw away, the other workers keep the short one, and the container waits longer than either."""

import asyncio
import re
from pathlib import Path

import pytest

pytest.importorskip("procrastinate")

from evo_agents.hub import jobs, worker  # noqa: E402

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "hub" / "docker-compose.yml"


def graces_passed(monkeypatch, queues):
    seen = []

    async def run_worker_async(**options):
        seen.append((options.get("queues"), options["shutdown_graceful_timeout"]))

    monkeypatch.setattr(worker.queue, "run_worker_async", run_worker_async)
    asyncio.run(worker.workers(queues=queues, shutdown_graceful_timeout=worker.shutdown_grace(queues)))
    return seen


def test_shutdown_grace_of_the_kg_worker_is_the_kg_grace_and_the_others_keep_25_s(monkeypatch):
    assert worker.KG_SHUTDOWN_GRACE > 440  # the median build
    assert graces_passed(monkeypatch, [jobs.KG_QUEUE]) == [([jobs.KG_QUEUE], worker.KG_SHUTDOWN_GRACE)]
    every = graces_passed(monkeypatch, None)
    assert len(every) == 4 and {grace for _, grace in every} == {worker.SHUTDOWN_GRACE} == {25.0}
    assert worker.shutdown_grace([jobs.DEFAULT_QUEUE, jobs.KG_QUEUE]) == worker.SHUTDOWN_GRACE


def test_shutdown_grace_of_worker_kg_is_below_its_container_stop_grace_period():
    service = re.split(r"\n  [\w-]+:\n", COMPOSE.read_text().split("\n  worker-kg:\n", 1)[1], maxsplit=1)[0]
    period = re.search(r"^    stop_grace_period: (\d+)s$", service, re.MULTILINE)
    assert period and int(period.group(1)) > worker.KG_SHUTDOWN_GRACE
