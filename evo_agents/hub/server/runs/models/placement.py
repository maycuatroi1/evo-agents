"""What a dispatch says of where its run goes (plan worker-pools, criterion a3): the labels it asks of the worker that
claims it (``labels``, and ``pool``, which asks pool=<name>) and the taints it tolerates. A claim takes the run only on
a worker whose labels hold every label it asks and each of whose taints it tolerates; a run pinned to a worker by name
goes there whatever its taints (``evo_agents.hub.server.worker_placement``). The run keeps them as ``selector`` and
``tolerations`` (schema 0149), lowercased, each once."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

# A label a run asks, or a taint it tolerates: a word, or key=value; a value may name a plugin's runtime, a/b.
PLACEMENT_LABEL = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}(=[A-Za-z0-9][A-Za-z0-9._/-]{0,99})?$"
POOL_KEY = "pool"
MAX_SELECTOR = 16

Label = Annotated[str, Field(pattern=PLACEMENT_LABEL)]
PoolName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")]


def normalized(values) -> list[str]:
    """Labels or taints lowercased, each once, in their order."""
    return list(dict.fromkeys(value.lower() for value in values or ()))


def selector_of(pool: str | None, labels) -> list[str]:
    """The labels a run asks: pool=<pool> when it names a pool, then ``labels``."""
    asked = [f"{POOL_KEY}={pool}"] if pool else []
    return normalized([*asked, *(labels or ())])


PLACED_BY_HUB = ("worker_id", "worker", "pool", "labels", "tolerations")
PLACED_BY_HUB_HINT = (
    "the hub places each run of an agent on the nodes of the agent's own pool, so a dispatch names no worker, pool, "
    "labels or tolerations: leave them out (an older evo-agents CLI sends them: update it), and ask an admin of the "
    "project to add a node to the pool of the agent (`evo-agents worker pool token create agent-<handle>`)"
)


class HubPlaced(BaseModel):
    """A dispatch body that places nothing: the hub does, on the pool of the run's agent. A body that names a worker,
    a pool, labels or tolerations is refused with a 422 that says so, not read as if it had none."""

    @model_validator(mode="before")
    @classmethod
    def _no_placement(cls, data):
        if isinstance(data, dict) and (named := [key for key in PLACED_BY_HUB if data.get(key) is not None]):
            raise ValueError(f"{', '.join(named)} cannot be set: {PLACED_BY_HUB_HINT}")
        return data


class RunPlacement(BaseModel):
    """What a run keeps of where it goes."""

    selector: list[str] = Field(
        [], description="the labels the worker that takes it must have, every one (pool=<name> for a pool); [] for any"
    )
    tolerations: list[str] = Field([], description="the taints of a worker it tolerates")
