"""What the lease of a run on a gateway model holds (``service.leases``): its constants, and what ``prepare`` found
for ``record`` to write."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import SecretStr

from evo_agents.hub.server.gateway.models.accounts import Account

GRACE = 300  # seconds a run's key outlives its timeout: the heartbeat that ends it, and the sweep after
TOKEN_PREFIX = "evr_"  # a run's token to the hub's gateway route
SWEEP_BATCH = 100


@dataclass
class Prepared:
    """What ``prepare`` found for a run on a gateway model, for ``record`` to write."""

    run_id: int
    prefix: str
    model: str
    api_format: str
    base_url: str
    accounts: list[Account] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    held: int | None = None  # the key row the run holds already for these accounts
    key_id: str | None = None  # a key made now
    key: SecretStr = field(default=SecretStr(""), repr=False)
    expires_at: datetime | None = None

    def audit_text(self) -> str:
        """How the credential.lease audit row names the provider accounts the run's key reaches: by id."""
        ids = ",".join(str(account.secret_id) for account in sorted(self.accounts, key=lambda a: a.secret_id))
        return f" provider-accounts={ids}" if ids else ""
