"""What the account rules of a run on a gateway model read and answer (``service.accounts``): the run that asks, the
provider accounts it may use, and why the others are left out."""

from __future__ import annotations

from dataclasses import dataclass, field

from evo_agents.hub import profiles

NOTHING = "nothing was dispatched"


@dataclass(frozen=True)
class Asker:
    """What the rules read of a run, or of a dispatch before its run exists (id 0)."""

    id: int
    owner_id: int  # who dispatched it
    principal: int  # who it acts for
    project_id: int
    profile_id: int | None = None
    profile_project_id: int | None = None
    agent_owned: bool = False  # an agent with a name, whose own accounts its runs take
    permissions: dict | None = None  # its profile's, whose credential axis names the accounts it may use

    def allows(self, name: str) -> bool:
        return profiles.allows(self.permissions, "credential", name)


@dataclass(frozen=True)
class Account:
    secret_id: int
    name: str
    share_id: int | None = None
    agent: bool = False


@dataclass
class Usable:
    accounts: list[Account] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # why an account of the prefix is left out

    def why(self, prefix: str) -> str:
        found = "; ".join(dict.fromkeys(self.reasons))
        return f"no provider account of {prefix} that this run may use" + (f": {found}" if found else "")
