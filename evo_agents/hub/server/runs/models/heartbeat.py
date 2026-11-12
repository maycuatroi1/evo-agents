"""A worker's heartbeat: what it reports of its machine, runtimes, checkouts and duties, and the hub's answer."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import runs
from evo_agents.hub.server.runs.models import browser as browser_help
from evo_agents.hub.server.runs.models.browser import BrowserInstallReport, BrowserInstallRequest, BrowserReport
from evo_agents.hub.server.runs.models.limits import (
    CHECKOUT_KEY,
    LINE,
    MAX_FEATURES,
    MAX_HEARTBEAT_RUNS,
    MAX_ID,
    MAX_REPORT_BYTES,
    MAX_RUN_KINDS,
    MAX_SELF_UPDATE_ERROR,
    MAX_SELF_UPDATE_REASON,
    OBJECT_NAME,
    RUN_KIND,
    ModelName,
    RuntimeName,
    json_bytes,
)
from evo_agents.hub.server.runs.models.run_parts import PluginRuntime


class RuntimeReport(BaseModel):
    """One runtime as the daemon found it on the machine."""

    available: bool = Field(description="whether runs may use it; false with a reason when it is installed but not")
    version: str | None = Field(None, min_length=1, max_length=100, pattern=LINE)
    reason: str | None = Field(None, min_length=1, max_length=500, pattern=LINE, description="why it is unavailable")
    models: list[ModelName] | None = Field(
        None,
        max_length=runs.MAX_RUNTIME_MODELS,
        description="the models the runtime lists on the machine, for a dispatch to suggest; null when it lists none",
    )


class CheckoutReport(BaseModel):
    """One checkout of a project's repo on the machine, which the daemon makes worktrees from."""

    path: str = Field(min_length=1, max_length=1024, pattern=LINE)
    branch: str | None = Field(None, min_length=1, max_length=255, pattern=LINE, description="checked out there")


class UpdateNowReport(BaseModel):
    """Where the daemon stands with its owner's Update now (update_now of the heartbeat answer)."""

    requested_at: datetime = Field(description="the request it is about: requested_at of the answer's update_now")
    ended: str | None = Field(
        None,
        pattern=RUN_KIND,
        description="null while the daemon carries it out; failed, the install failed (self_update.error says how); "
        "impossible, the daemon cannot update itself (self_update.reason says why). The hub ends the request with "
        "either, and with a value it does not know ends nothing",
    )


class SelfUpdateReport(BaseModel):
    """What the daemon last found of updating itself to the tag nightly of evo-agents."""

    state: str = Field(
        pattern=RUN_KIND,
        description="current, it runs the commit nightly points to; waiting, nightly points to another one and it "
        "waits until every daemon of its install is idle; updating, it is installing that commit and restarts on it; "
        "locked, its owner locked the worker's version; unsupported, it cannot update itself (reason says why). A "
        "state this hub does not know is kept as named (runs.SELF_UPDATE_STATES)",
    )
    reason: str | None = Field(
        None, min_length=1, max_length=MAX_SELF_UPDATE_REASON, description="why, with unsupported above all"
    )
    nightly_commit: str | None = Field(
        None, pattern=OBJECT_NAME, description="the commit nightly pointed to when the daemon last read it"
    )
    checked_at: datetime | None = Field(None, description="when the daemon last read where nightly points")
    error: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_SELF_UPDATE_ERROR,
        description="the last attempt that failed: reading the tag, installing, or the new install not starting",
    )
    error_at: datetime | None = Field(None, description="when that attempt failed")
    update_now: UpdateNowReport | None = Field(
        None,
        description="for a daemon that says update_now in its features, where it stands with the owner's Update now "
        "the answer carried; null when it carries none",
    )


class DiskReport(BaseModel):
    """The free space of the drive that holds the daemon's worktrees."""

    free_gb: float = Field(ge=0, description="gigabytes free there")
    free_pct: float = Field(ge=0, le=100, description="the share of the drive free, in percent")
    reserve_gb: float | None = Field(
        None,
        ge=0,
        description="the gigabytes the daemon keeps free there (disk_reserve_gb of its config): under it the hub gives "
        "the worker no run; null from a daemon that says none, for which the hub's own reserve of 20 holds",
    )


class HealthReport(BaseModel):
    """What the daemon last found of its machine's health (plan hub-steward, criterion a3): the free disk where its
    worktrees go, and the login of each runtime as the runtime's own status command says it, never a credential."""

    disk: DiskReport | None = Field(None, description="null when the daemon could not read it")
    logins: dict[RuntimeName, Annotated[str, Field(pattern=RUN_KIND)]] = Field(
        default_factory=dict,
        max_length=runs.MAX_RUNTIME_LOGINS,
        description="each runtime's login: ok, signed in; unauthorized, its status command says it is not; missing, "
        "its program is not on the machine; unknown, the command gave no answer in time or one the daemon cannot read",
    )


KG_PROJECT = r"^[a-z0-9][a-z0-9-]{0,99}$"  # a project's name, as evo_agents.hub.server.admin.PROJECT_NAME
KgSyncError = Annotated[str, Field(min_length=1, max_length=runs.KG_SYNC_ERROR_CHARS)]


class KgSyncSource(BaseModel):
    """What the daemon last did for one git source of a project whose night shift it is on duty for."""

    source: str = Field(min_length=1, max_length=200, pattern=LINE, description="the source's id in knowledge.yaml")
    status: str = Field(
        pattern=RUN_KIND,
        description="ok, the source was read, or found at the commit it read last, and every run of it is on the hub; "
        "failed, reading it, merging the hub's runs of it or pushing went wrong (error says how); skipped, the worker "
        "does not read it (error says why). A status this hub does not know is kept as named (runs.KG_SYNC_STATES)",
    )
    error: KgSyncError | None = None
    ref: str | None = Field(None, min_length=1, max_length=255, pattern=LINE, description="the ref it reads")
    commit: str | None = Field(None, pattern=OBJECT_NAME, description="the commit its last ok read read")
    behind: int | None = Field(
        None, ge=0, description="commits of the remote's default branch that commit lacked when it was read"
    )
    checked_at: datetime | None = Field(None, description="when the daemon last looked at the source")
    read_at: datetime | None = Field(None, description="when the commit it holds was last read")
    pushed_at: datetime | None = Field(None, description="when it last pushed a run of the source")
    pulled: int = Field(0, ge=0, description="runs of other machines the last look merged from the hub")
    pushed: int = Field(0, ge=0, description="runs the last look pushed")


class KgSyncProject(BaseModel):
    """What the daemon last did for one project whose night shift it is on duty for."""

    project: str = Field(pattern=KG_PROJECT)
    checked_at: datetime = Field(description="when the daemon last looked at the project")
    error: KgSyncError | None = Field(
        None,
        description="what stopped the look before any source: no checkout of the harness here, a knowledge.yaml that "
        "does not load, or a hub that refused the push",
    )
    sources: list[KgSyncSource] = Field(default_factory=list, max_length=runs.MAX_KG_SYNC_SOURCES)


class KgSyncReport(BaseModel):
    """What a daemon of feature kg_sync last did to keep the graphs of the projects it is on duty for fresh."""

    projects: list[KgSyncProject] = Field(default_factory=list, max_length=runs.MAX_KG_SYNC_PROJECTS)


class KgDuty(BaseModel):
    """A project whose graph the daemon keeps fresh: its night shift names this worker, which serves it."""

    project: str
    received_at: datetime | None = Field(
        description="when the hub last took a run of the project's graph, from any machine: a daemon that saw an "
        "earlier time merges the hub's runs and looks at the project's sources again; null when it holds none"
    )


class HeartbeatRequest(BaseModel):
    runtimes: dict[RuntimeName, RuntimeReport] = Field(
        default_factory=dict,
        description='the runtimes on the machine: {"claude-code": {"available": true, "version": "2.1.289"}}, and '
        "those of the plugins its owner accepted that the hub sent it (plugin_runtimes of the answer), as "
        "<plugin>/<name>",
    )
    checkouts: dict[Annotated[str, Field(pattern=CHECKOUT_KEY)], CheckoutReport] = Field(
        default_factory=dict,
        max_length=500,
        description='keyed <project>/<repo>: {"evo-agents/evo-agents": {"path": "/src/evo-agents", "branch": "main"}}',
    )
    free_slots: int = Field(ge=0, le=runs.MAX_SLOTS)
    runs: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        default_factory=list, max_length=MAX_HEARTBEAT_RUNS, description="the runs the worker holds"
    )
    orphans: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        default_factory=list,
        max_length=MAX_HEARTBEAT_RUNS,
        description="of runs, those a previous daemon left, whose agent this one stops whatever the answer: the hub "
        "re-attaches none of them. An older daemon names none, and lists them among runs alone",
    )
    agent_version: str | None = Field(None, min_length=1, max_length=100, pattern=LINE)
    run_kinds: list[Annotated[str, Field(pattern=RUN_KIND)]] | None = Field(
        None,
        max_length=MAX_RUN_KINDS,
        description="the kinds of run the daemon runs; one that says none takes no review run. A kind this hub does "
        "not know is left out",
    )
    features: list[Annotated[str, Field(pattern=RUN_KIND)]] | None = Field(
        None,
        max_length=MAX_FEATURES,
        description="what the daemon does besides its kinds of run: session_per_step, a plan run with a session for "
        "each step; author_decisions, an author run whose agent asks decisions and waits for the answers of a turn; "
        "agent_profiles, a run whose agent profile limits tool, skill or model, or has instructions, applied in the "
        "agent session; plugin_runtimes, a run of a plugin's runtime through its adapter of ACP; environments, a run "
        "whose agent runs in a container of a plugin's environment. One that says none "
        "takes no such run, and its author run's agent asks in the chat alone. A feature this hub does not know is "
        "left out",
    )
    agent_commit: str | None = Field(
        None,
        pattern=OBJECT_NAME,
        description="the commit of evo-agents the daemon's install was built from; none for an install that names "
        "no commit (PyPI, a local checkout) and from an older daemon. Each heartbeat sets what the hub shows",
    )
    self_update: SelfUpdateReport | None = Field(
        None,
        description="what the daemon last found of updating itself to the tag nightly; each heartbeat sets what the "
        "hub shows, so a daemon sends it every time once it has one",
    )
    kg_sync: KgSyncReport | None = Field(
        None,
        description="for a daemon of feature kg_sync: what it last did for each project of kg_sync in the answer; each "
        "heartbeat sets what the hub shows, and none clears it, as from a daemon whose sync is off",
    )
    browser: BrowserReport | None = Field(None, description=browser_help.REPORT_HELP)
    browser_install: BrowserInstallReport | None = Field(None, description=browser_help.INSTALL_REPORT_HELP)
    health: HealthReport | None = Field(
        None,
        description="the free disk of its worktrees' drive and each runtime's login, as the daemon last looked; each "
        "heartbeat sets what the hub shows, and none clears it, as from an older daemon",
    )

    def stored(self) -> tuple[dict, dict]:
        """runtimes and checkouts as workers keeps them and GET /v1/workers/{id} shows them: every key present."""
        return (
            {name: report.model_dump() for name, report in self.runtimes.items()},
            {key: report.model_dump() for key, report in self.checkouts.items()},
        )

    @model_validator(mode="after")
    def _bounded(self):
        runtimes, checkouts = self.stored()
        if json_bytes(runtimes) + json_bytes(checkouts) > MAX_REPORT_BYTES:
            raise ValueError(f"runtimes and checkouts take at most {MAX_REPORT_BYTES} bytes of JSON")
        return self


def available(report) -> bool:
    """Whether a stored runtime may take runs: an object with available true (or without the key, as a version
    alone), true, or a version string."""
    if isinstance(report, dict):
        return report.get("available", True) is True
    return report is True or (isinstance(report, str) and bool(report))


class RunControl(BaseModel):
    id: int
    held: bool = Field(description="false for a run the worker no longer holds; stop it")
    state: runs.RunState | None = Field(description="null for a run that is not this worker's")
    lease_expires_at: datetime | None
    cancel: bool
    takeover: bool = Field(
        default=False, description="the owner asked to drive the agent in a terminal: report interactive"
    )
    handback: bool = Field(default=False, description="the owner asked to let the agent go on headless: report running")
    terminal_open: bool = Field(
        default=False,
        description="a browser waits for the run's terminal: connect WS /v1/worker/runs/{id}/terminal once the run "
        "is interactive",
    )
    park: bool = Field(
        default=False,
        description="the run is parked (it waited too long for an answer), or done because a new run resumes it: stop "
        "the agent at the end of its turn, keep the session and the worktrees, free the slot",
    )
    finish: bool = Field(
        default=False,
        description="the owner ended the chat of this author run: once the agent's turn is over, end the run done",
    )
    inbox: int = Field(default=0, description="messages from the owner waiting for the agent: POST .../runs/{id}/inbox")
    decisions: int = Field(default=0, description="decisions of the run still open, waiting for the owner's answer")
    renew: bool = Field(
        default=False,
        description="the hub lost the run while this worker was silent and re-attached it, and the leases it held "
        "ended: take the run's credentials again",
    )


class WorkerSettings(BaseModel):
    slots: int = Field(ge=1, le=runs.MAX_SLOTS, description="runs the worker holds at once")
    name: str
    labels: list[str]
    plugins: list[str] = Field(
        default_factory=list,
        description="the plugins whose runtimes and environments the worker takes; a hub before 0039 sends none",
    )
    version_locked: bool = Field(
        description="its owner locked its version: the daemon stays on the evo-agents it runs and does not update "
        "itself; a hub before 0048 sends none, and a daemon that got none does not update itself either"
    )
    projects: list[str] = Field(
        default_factory=list,
        description="the projects the worker serves, by name, as its owner set them; a hub before evo-ak 0.31.2 sends "
        "none, and a daemon that got none keeps those of its config",
    )
    guard: str = Field(
        "",
        description="the digest of the hub's guard policy (GET /v1/guard/policy), empty when it guards nothing: the "
        "daemon fetches the policy when the digest differs from the one it holds; a hub before 0192 sends none",
    )


class NightlyRead(BaseModel):
    """Where the tag nightly of evo-agents pointed when the hub last read it (evo_agents.hub.nightly)."""

    commit: str = Field(pattern=OBJECT_NAME)
    read_at: datetime = Field(description="when the hub read it; the hub reads it once a minute")


class UpdateNowRequest(BaseModel):
    """An Update now of the worker's owner, open until the daemon runs nightly's commit or the request ends."""

    requested_at: datetime = Field(description="when it was asked for; it names the request in self_update.update_now")


def _absent(value) -> bool:
    return value is None


class HeartbeatAnswer(BaseModel):
    drain: bool = Field(description="claim no new run; finish the ones held")
    runs: list[RunControl]
    settings: WorkerSettings = Field(
        description="the worker's settings as the hub holds them, which its owner may change (PATCH "
        "/v1/workers/{id}): a daemon whose config says otherwise takes these; a hub before schema 0026 sends none"
    )
    plugin_runtimes: list[PluginRuntime] = Field(
        default_factory=list,
        description="for a daemon that says plugin_runtimes: the runtimes of the plugins of settings.plugins that a "
        "project the worker serves enabled, which it reports in runtimes once it found their programs; empty otherwise",
    )
    kg_sync: list[KgDuty] = Field(
        default_factory=list,
        description="for a daemon that says kg_sync: the projects it serves whose night shift names it and whose graph "
        "is on, whose git sources it syncs and pushes (evo_agents.worker.kg_sync); empty otherwise",
    )
    nightly: NightlyRead | None = Field(
        None,
        exclude_if=_absent,
        description="the commit the tag nightly pointed to when the hub last read it, and when: a daemon may take it "
        "instead of reading the tag itself. Absent until the hub has read it once, and from a hub before schema 0065",
    )
    update_now: UpdateNowRequest | None = Field(
        None,
        exclude_if=_absent,
        description="the owner asked the daemon to update itself now: claim no new run, let the held ones end, update "
        "to nightly and claim again. Absent when no request is open; sent only to a daemon that says update_now",
    )
    browser_install: BrowserInstallRequest | None = Field(
        None, exclude_if=_absent, description=browser_help.INSTALL_REQUEST_HELP
    )
