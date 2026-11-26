"""``evo-agents hub admin``: grants, users, row counts, the hub's channels, invites and the admin view of a project.

:data:`ADMIN` is the group, listing its children by hand: ``invite`` (``cli.admin.invites``, over
``server.invites``), ``project`` (``cli.admin.project``, a self-managed project), ``kg-pool`` (``cli.kg.pool``),
``grant``, ``revoke``, ``users`` and ``stats`` (``cli.admin.users``, over ``server.admin``), and ``telegram``,
``telegram-unlink``, ``channel-link`` and ``channel-unlink`` (``cli.admin.channels``). Their gateways are in
``cli.admin.resources``. The audit trail and every user's tokens (``server.admin_console``) have no command and are
read in the web's admin area. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Group
from evo_agents.hub.cli.admin.channels import CHANNEL_LINK, CHANNEL_UNLINK, TELEGRAM, TELEGRAM_UNLINK
from evo_agents.hub.cli.admin.invites import INVITE
from evo_agents.hub.cli.admin.project import PROJECT
from evo_agents.hub.cli.admin.users import GRANT, REVOKE, STATS, USERS
from evo_agents.hub.cli.kg.pool import KG_POOL

ADMIN = Group(
    "admin",
    "users, grants and invite links (hub admins only)",
    [INVITE, PROJECT, KG_POOL, GRANT, REVOKE, USERS, STATS, TELEGRAM, TELEGRAM_UNLINK, CHANNEL_LINK, CHANNEL_UNLINK],
)
