"""What a dispatch says of where its run goes (plan worker-pools, criterion a3): the labels it asks of the worker that
claims it (``labels``, and ``pool``, which asks pool=<name>) and the taints it tolerates. A claim takes the run only on
a worker whose labels hold every label it asks and each of whose taints it tolerates; a run pinned to a worker by name
goes there whatever its taints (``evo_agents.hub.server.worker_placement``). The run keeps them as ``selector`` and
``tolerations`` (schema 0148), lowercased, each once."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, field_validator

# A label a run asks, or a taint it tolerates: a word, or key=value; a value may name a plugin's runtime, a/b.
PLACEMENT_LABEL = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}(=[A-Za-z0-9][A-Za-z0-9._/-]{0,99})?$"
POOL_KEY = "pool"
MAX_SELECTOR = 16

Label = Annotated[str, Field(pattern=PLACEMENT_LABEL)]
PoolName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")]

POOL_HELP = "only a node of the pool of this name takes the run: the same as asking the label pool=<name>"
LABELS_HELP = (
    "labels the worker that takes the run must have, every one: its own, its pool's, pool=<name>, and the ones the "
    "hub gives it from its heartbeat (os=, arch=, runtime=<name>, feature=browser)"
)
TOLERATIONS_HELP = (
    "taints of a worker the run tolerates: a worker with a taint the run does not tolerate leaves it to others, "
    "unless the run is pinned to it"
)


def normalized(values) -> list[str]:
    """Labels or taints lowercased, each once, in their order."""
    return list(dict.fromkeys(value.lower() for value in values or ()))


def selector_of(pool: str | None, labels) -> list[str]:
    """The labels a run asks: pool=<pool> when it names a pool, then ``labels``."""
    asked = [f"{POOL_KEY}={pool}"] if pool else []
    return normalized([*asked, *(labels or ())])


class PlacementFields(BaseModel):
    """The fields of a dispatch that place its run; each optional."""

    pool: PoolName | None = Field(None, description=POOL_HELP)
    labels: list[Label] | None = Field(None, max_length=MAX_SELECTOR, description=LABELS_HELP)
    tolerations: list[Label] | None = Field(None, max_length=MAX_SELECTOR, description=TOLERATIONS_HELP)

    @field_validator("labels", "tolerations")
    @classmethod
    def _lowered(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalized(value)

    def selector(self) -> list[str]:
        return selector_of(self.pool, self.labels)

    def placed(self) -> bool:
        """Whether it names any of them: a rerun that names none keeps the run's own."""
        return self.pool is not None or self.labels is not None or self.tolerations is not None


class RunPlacement(BaseModel):
    """What a run keeps of where it goes."""

    selector: list[str] = Field(
        [], description="the labels the worker that takes it must have, every one (pool=<name> for a pool); [] for any"
    )
    tolerations: list[str] = Field([], description="the taints of a worker it tolerates")
