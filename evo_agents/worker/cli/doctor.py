"""``evo-agents worker doctor [--json]``: what this machine holds or allows that a worker should not, as the checks
of ``evo_agents.worker.doctor`` find it; exit status 2 while a finding is high."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.worker import doctor
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext


class Doctor(WorkerCommand):
    name = "doctor"
    help = (
        "what this machine holds that a worker should not: other administrators, a readable home, long-lived "
        "credentials, a PATH others write; exits 2 when a finding is high"
    )
    arguments = (Arg("--json", action="store_true", help="machine-readable output"),)

    def run(self, ctx: WorkerContext) -> Result:
        report = doctor.examine(doctor.Machine.here())
        return Result(report.to_json(), Text(*doctor.report_lines(report)), report.exit_status())

    def failed(self, exc: Exception) -> int | None:
        return None  # the doctor reads the machine alone: no failure of the hub or the worker's state is its own


DOCTOR = Doctor()
