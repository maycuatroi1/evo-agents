"""The models of the worker API of ``evo_agents.hub.server.workers``: what a pairing, a join and a registration take,
what a worker shows (GET /v1/workers, GET /v1/workers/{id}) and what PATCH /v1/workers/{id} changes, with the bounds
of names, labels and one-line texts that other modules of the hub check the same way."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import runs
from evo_agents.hub.credentials import DISPATCH_FROM
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.runs.models import KgSyncReport, NightlyRead, SelfUpdateReport
from evo_agents.plugins.manifest import PLUGIN_ID

WORKER_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"  # as the workers table accepts it
LABEL = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"
LINE = r"^[^\x00-\x1f\x7f]+$"  # one line, without control characters
MAX_LABELS = 16


class WorkerScope(BaseModel):
    name: str = Field(pattern=WORKER_NAME, description="unique among the owner's workers that are not revoked")
    projects: list[Annotated[str, Field(pattern=PROJECT_NAME)]] = Field(
        min_length=1, max_length=100, description="projects the worker takes runs of; the writer role on each"
    )
    slots: int = Field(1, ge=1, le=runs.MAX_SLOTS, description="runs the worker holds at once")
    labels: list[Annotated[str, Field(pattern=LABEL)]] = Field(default_factory=list, max_length=MAX_LABELS)
    allow_web_terminal: bool = Field(False, description="whether the owner may open a terminal on it from the web")


class HostFacts(BaseModel):
    hostname: str = Field(min_length=1, max_length=255, pattern=LINE)
    os: str = Field(min_length=1, max_length=40, pattern=LINE)
    arch: str = Field(min_length=1, max_length=40, pattern=LINE)
    agent_version: str = Field(min_length=1, max_length=100, pattern=LINE, description="of the evo-agents daemon")


class PairingRequest(WorkerScope):
    pass


class WorkerRegistration(WorkerScope, HostFacts):
    pass


class JoinRequest(HostFacts):
    code: str = Field(min_length=1, max_length=32, description="the pairing code, XXXX-XXXX")


class Pairing(BaseModel):
    id: int
    code: str = Field(description="XXXX-XXXX; shown once, the hub keeps only its hash and its first four characters")
    expires_at: datetime
    name: str
    projects: list[str]
    slots: int
    labels: list[str]
    allow_web_terminal: bool


class PairingState(BaseModel):
    id: int
    status: Literal["waiting", "joined", "expired", "locked"]
    name: str
    projects: list[str]
    slots: int
    labels: list[str]
    allow_web_terminal: bool
    tries_left: int = Field(description="wrong codes the pairing takes before it locks")
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    worker_id: int | None = Field(description="the worker the pairing made, once a machine joined with it")


class WorkerSelfUpdate(SelfUpdateReport):
    """What a worker's daemon last said of updating itself to the tag nightly, as the hub keeps it."""

    since: datetime | None = Field(
        description="when the hub first got this state, in an unbroken line of heartbeats that said it: how long the "
        "daemon has been waiting, for one"
    )


class WorkerUpdateNow(BaseModel):
    """The last Update now of a worker: open while ended_at is null (POST /v1/workers/{id}/update-now)."""

    requested_at: datetime = Field(description="when its owner or a hub admin asked for it")
    ended_at: datetime | None = Field(description="when it ended; null while the daemon carries it out")
    outcome: Literal[runs.UPDATE_NOW_OUTCOMES] | None = Field(
        description="how it ended: current, the daemon runs the commit of nightly the hub read; cancelled; failed, "
        "the install failed (self_update.error says how); impossible, the daemon cannot update itself; null while open"
    )
    reason: str | None = Field(description="why it failed or was impossible, as the daemon or the hub said")


class Worker(BaseModel):
    id: int
    name: str
    owner: str = Field(description="the login of the member who registered it")
    status: Literal[runs.WORKER_STATUSES]
    hostname: str
    os: str
    arch: str
    agent_version: str
    agent_commit: str | None = Field(
        description="the commit of evo-agents its daemon's install was built from, as its last heartbeat said; null "
        "when that heartbeat named none: an older daemon, or an install from PyPI or a local checkout"
    )
    slots: int
    labels: list[str]
    plugins: list[str] = Field(
        description="the plugins whose runtimes and environments it takes, as its owner set them"
    )
    version_locked: bool = Field(
        description="its owner locked its version: its daemon stays on the evo-agents it runs and does not update "
        "itself to the tag nightly"
    )
    shared: bool = Field(
        False,
        description="its owner shares it: it also takes the runs of every member who holds writer on a project it "
        "serves, one member's runs at a time; listed to those members too, who see it but cannot change it",
    )
    self_update: WorkerSelfUpdate | None = Field(
        description="what its daemon last said of updating itself to the tag nightly; null when its last heartbeat "
        "said nothing of it, as an older daemon does"
    )
    kg_sync: KgSyncReport | None = Field(
        None,
        description="what its daemon last said of keeping fresh the graphs of the projects whose night shift it is on "
        "duty for: each project and git source, with the error of a sync that failed; null when its last heartbeat "
        "said nothing of it",
    )
    update_now: WorkerUpdateNow | None = Field(
        description="the last Update now its owner or a hub admin asked for, open or ended; null when none was"
    )
    nightly: NightlyRead | None = Field(
        description="where the tag nightly pointed when the hub last read it, the same for every worker; null until "
        "the hub has read it once"
    )
    projects: list[str]
    runtimes: dict = Field(
        description="the runtimes its last heartbeat reported, keyed claude-code, opencode, codex or <plugin>/<name>, "
        "each {available, version, reason, models}; models lists what the runtime offers there, null when it lists none"
    )
    checkouts: dict = Field(
        description="the checkouts its last heartbeat reported, keyed <project>/<repo>, each {path, branch}"
    )
    free_slots: int | None = Field(description="the free slots its last heartbeat reported; null before the first")
    allow_web_terminal: bool
    dispatch_from: Literal[DISPATCH_FROM] = Field(
        description="who may hand it runs: any, runs its owner dispatched with any credential; web, only runs "
        "dispatched from a web session"
    )
    held_runs: int = Field(description="runs it holds now (leased, running, interactive or verifying)")
    features: list[str] = Field(
        default_factory=list,
        description="what its daemon said it does besides its kinds of run: session_per_step, a plan run with a "
        "session for each step; author_decisions, an author run whose agent asks decisions; empty for a daemon that "
        "said none",
    )
    created_at: datetime
    last_heartbeat_at: datetime | None
    drained_at: datetime | None
    revoked_at: datetime | None


class DispatchFrom(BaseModel):
    value: Literal[DISPATCH_FROM] = Field(
        description="any: runs dispatched with any credential of the owner; web: only runs dispatched from a web "
        "session, so a token cannot hand the worker work"
    )


class WorkerUpdate(BaseModel):
    """What PATCH /v1/workers/{id} changes: each field it gives, at least one; a field left out or null stays."""

    slots: int | None = Field(None, ge=1, le=runs.MAX_SLOTS, description="runs the worker holds at once")
    labels: list[Annotated[str, Field(pattern=LABEL)]] | None = Field(None, max_length=MAX_LABELS)
    name: str | None = Field(None, pattern=WORKER_NAME, description="unique among the owner's workers not revoked")
    plugins: list[Annotated[str, Field(pattern=PLUGIN_ID)]] | None = Field(
        None,
        max_length=runs.MAX_WORKER_PLUGINS,
        description="the plugins whose runtimes and environments it takes; [] for none",
    )
    version_locked: bool | None = Field(
        None,
        description="true: its daemon stays on the evo-agents it runs; false: it updates itself to the tag nightly "
        "again, once idle",
    )
    shared: bool | None = Field(
        None,
        description="true: it also takes the runs of every member who holds writer on a project it serves; false: "
        "its owner's runs alone, every worker's default",
    )

    @model_validator(mode="after")
    def _some_field(self) -> WorkerUpdate:
        fields = (self.slots, self.labels, self.name, self.plugins, self.version_locked, self.shared)
        if all(value is None for value in fields):
            raise ValueError("name at least one of slots, labels, name, plugins, version_locked and shared to change")
        return self


class WorkerCredential(BaseModel):
    token: str = Field(description="the worker token, evw_...; shown once, the hub keeps only its SHA-256")
    token_id: int
    expires_at: datetime = Field(description="moves forward with every use")
    worker: Worker
