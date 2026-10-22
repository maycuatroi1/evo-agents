"""The daemon's doctor report (``evo_agents.worker.doctor_report``): what it sends of the doctor's findings (code,
severity and the detail redacted, nothing else), when (at start, then every ``every`` seconds until the daemon stops),
and that neither a hub without the route (404) nor a check that raises ends the loop. The hub is a fake object that
records each body; the examined machine is a function of the test."""

from __future__ import annotations

import asyncio
import logging
import secrets as token_bytes

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import runs  # noqa: E402
from evo_agents.worker import doctor, doctor_report  # noqa: E402
from evo_agents.worker.hubapi import HubProblem, Refused  # noqa: E402


def a_report(*findings: doctor.Finding) -> doctor.Report:
    return doctor.Report(user="ann", home="/home/ann", platform="linux", path="/usr/bin", findings=list(findings))


class FakeHub:
    """Records each doctor body; answers with ``fail`` (an exception) when set."""

    def __init__(self, fail: Exception | None = None):
        self.bodies: list[dict] = []
        self.fail = fail

    async def doctor(self, body: dict) -> dict:
        self.bodies.append(body)
        if self.fail is not None:
            raise self.fail
        return {"worker_id": 1, "doctor_high": 0, "doctor_medium": 0}


def test_doctor_report_is_a_feature_every_daemon_says():
    assert runs.DOCTOR_REPORT == "doctor_report" and runs.DOCTOR_REPORT in runs.WORKER_FEATURES


def test_doctor_report_payload_carries_code_severity_and_redacted_detail_only():
    token = "evw_" + token_bytes.token_urlsafe(24)
    gh = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
    report = a_report(
        doctor.Finding(doctor.MACHINE_TOKEN, doctor.MEDIUM, "/home/ann/.evo/hub/token", f"token {token}", "rotate it"),
        doctor.Finding(doctor.OTHER_ADMIN, doctor.HIGH, "carol", f"carol administers it, {gh}", "remove carol"),
    )
    body = doctor_report.payload(report, (token,))
    assert [(f["code"], f["severity"]) for f in body["findings"]] == [
        (doctor.OTHER_ADMIN, doctor.HIGH),
        (doctor.MACHINE_TOKEN, doctor.MEDIUM),
    ]
    assert all(set(f) == {"code", "severity", "detail"} for f in body["findings"])
    text = repr(body)
    assert token not in text and gh[4:] not in text and "remove carol" not in text and "/home/ann/.evo" not in text


def test_doctor_report_loop_sends_at_start_and_again_after_every_until_stopped():
    hub = FakeHub()
    report = a_report(doctor.Finding(doctor.OTHER_ADMIN, doctor.HIGH, "carol", "carol administers it", "fix"))

    async def run() -> None:
        stopping = asyncio.Event()
        loop = asyncio.create_task(doctor_report.report_loop(hub, stopping, lambda: report, every=0.05))
        while len(hub.bodies) < 2:
            await asyncio.sleep(0.01)
        stopping.set()
        await asyncio.wait_for(loop, 2)

    asyncio.run(run())
    assert len(hub.bodies) >= 2
    assert hub.bodies[0] == {
        "findings": [{"code": "other-admin", "severity": "high", "detail": "carol administers it"}]
    }


def test_doctor_report_a_hub_without_the_route_is_info_and_a_failure_a_warning(caplog):
    report = a_report()
    caplog.set_level(logging.INFO, logger="evo_agents.worker.doctor_report")
    old = FakeHub(Refused("not found", status=404))
    assert asyncio.run(doctor_report.send_once(old, lambda: report)) is None
    assert [r.levelno for r in caplog.records] == [logging.INFO] and old.bodies == [{"findings": []}]
    caplog.clear()
    down = FakeHub(HubProblem("hub unreachable", status=503))
    assert asyncio.run(doctor_report.send_once(down, lambda: report)) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_doctor_report_a_check_that_raises_sends_nothing_and_the_loop_goes_on(caplog):
    hub = FakeHub()
    calls = []

    def examine() -> doctor.Report:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("a bug of a check")
        return a_report()

    async def run() -> None:
        stopping = asyncio.Event()
        loop = asyncio.create_task(doctor_report.report_loop(hub, stopping, examine, every=0.01))
        while not hub.bodies:
            await asyncio.sleep(0.01)
        stopping.set()
        await asyncio.wait_for(loop, 2)

    asyncio.run(run())
    assert hub.bodies[0] == {"findings": []} and len(calls) >= 2
    assert "the doctor could not examine the machine" in caplog.text


def test_doctor_report_examines_the_home_of_the_daemons_environment(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(doctor, "examine", lambda machine: seen.append(machine) or a_report())
    doctor_report.examine_here({"HOME": str(tmp_path), "PATH": "/usr/bin"})
    assert seen[0].home == tmp_path and seen[0].env["HOME"] == str(tmp_path)
