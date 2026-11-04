"""The run API's limits and shared types: path and field types of its routes, the refusals they share, and the
messages and bounds the other models of the package check against."""

from __future__ import annotations

import json
from typing import Annotated, Literal, get_args

from fastapi import Path
from pydantic import Field

from evo_agents.hub import profiles, runs
from evo_agents.hub.credentials import MAX_SECRET_NAME_CHARS, SECRET_NAME
from evo_agents.hub.runs import MAX_EVIDENCE_BYTES
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.plugins.manifest import PLUGIN_ID

MAX_ID = 2**63 - 1  # bigint
MAX_DISPATCH_STEPS = 50
MAX_HEARTBEAT_RUNS = 64
MAX_REPORT_BYTES = 64 * 1024  # runtimes and checkouts of a heartbeat, or the usage of a state report, as JSON
LINE = r"^[^\x00-\x1f\x7f]+$"
OBJECT_NAME = r"^([0-9a-f]{40}|[0-9a-f]{64})$"
CHECKOUT_KEY = r"^[a-z0-9][a-z0-9-]{0,99}/[^\x00-\x1f\x7f/][^\x00-\x1f\x7f]{0,199}$"  # <project>/<repo>
STEP_KEY_CHARS = 200
RequestedRuntime = Literal["any", runs.Runtime]
REQUESTED_RUNTIMES: tuple[str, ...] = get_args(RequestedRuntime)
NOT_HELD = "this worker does not hold run {id}: it may have been lost, cancelled or taken by another attempt"
StepReportStatus = Literal["in_progress", "done", "pending"]
STEP_REPORT_STATUSES: tuple[str, ...] = get_args(StepReportStatus)  # what a plan run's worker reports of a step
RUN_KIND = r"^[a-z][a-z0-9_]{0,31}$"  # a kind of run a heartbeat names, known to this hub or not
MAX_RUN_KINDS = 20
MAX_SELF_UPDATE_REASON = runs.SELF_UPDATE_REASON_CHARS  # characters of self_update.reason in a heartbeat
MAX_SELF_UPDATE_ERROR = runs.SELF_UPDATE_ERROR_CHARS  # characters of self_update.error in a heartbeat
MAX_STEP_EVIDENCE_CHARS = MAX_EVIDENCE_BYTES  # the agent's evidence of one step; the hub's adds to it, within the bytes

RunId = Annotated[int, Path(ge=1, le=MAX_ID)]
# Any runtime's name, any included: one of the core's or a plugin's <plugin>/<name>. A dispatch takes one registered in
# its project (``service.plugin_runtimes``), else 422.
RuntimeName = Annotated[str, Field(pattern=runs.RUNTIME_NAME, max_length=127)]
RUNTIME_HELP = (
    "any: the claiming worker picks a runtime of the core it has; else claude-code, opencode, codex, or "
    "<plugin>/<name>, the runtime a plugin enabled on the project declares; a runtime the project does not register is "
    "422"
)
ModelName = Annotated[str, Field(min_length=1, max_length=runs.MAX_MODEL_CHARS, pattern=LINE)]
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}}
StepKey = Annotated[str, Path(min_length=1, max_length=STEP_KEY_CHARS, description="the step's id, or its order")]
AccountName = Annotated[str, Field(max_length=MAX_SECRET_NAME_CHARS, pattern=f"^{SECRET_NAME.pattern}$")]
ProfileHandle = Annotated[str, Field(pattern=profiles.HANDLE_PATTERN)]
PROFILE_HELP = (
    "the agent profile the run goes under, by handle (GET /v1/projects/{p}/agents): what its runs may use; the hub "
    "refuses a dispatch its allowlists leave out (422). Null: the project's default profile, which allows everything"
)
PluginKey = Annotated[str, Field(pattern=PLUGIN_ID)]
PLUGIN_CONFIG_HELP = (
    "the run's own layer of the config of plugins enabled on the project, above the agent profile's: plugin name to "
    "the plain values it sets, checked against the plugin's schema; no secret (422). Null: none"
)
ACCOUNT_HELP = (
    "one of the caller's env secrets bound to the project, by name (GET /v1/accounts): the run takes it for its "
    "variable and keeps it, with no failover to another account; null: the account is picked by the rules of the "
    "caller's policy for the project"
)
AS_ME_HELP = (
    "true: the run's agent calls the hub's API as you, with your grants on every project and hub admin if you are "
    "one, but never your secrets, tokens or workers, through a token of the run's own that ends with the run "
    "(evo_agents.hub.server.run_access); a run's own token may not ask it"
)

MAX_LIST = 200
MAX_OFFSET = 100_000
STATS_DAYS = 30  # the days GET .../runs/stats covers without ?days, today in UTC the last of them
MIN_STATS_DAYS, MAX_STATS_DAYS = 7, 90


def json_bytes(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())
