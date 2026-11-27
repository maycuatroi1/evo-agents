"""The shapes of what the hub finds for a credential request (``service.resolver``)."""

from __future__ import annotations

from dataclasses import dataclass, field

K12 = "K12"


@dataclass
class Found:
    """The owner's secrets of kind env for a request's variable, by what the run's lease makes of them, each list in the
    order the hub prefers them: the name the request proposes first, then by name."""

    env_var: str
    project: str
    leased: list[str] = field(default_factory=list)
    bindable: list[tuple[int, str]] = field(default_factory=list)  # (id, name)
    withheld: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.leased or self.bindable or self.withheld)
