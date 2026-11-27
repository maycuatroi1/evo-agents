"""The publishers whose signed plugin bundles the hub accepts (plan plugin-market, criterion a6): those the operator
lists in EVO_HUB_PLUGIN_PUBLISHERS, and the signers hub admins trusted from the web or the CLI as they installed from a
market or a GitHub release (``plugin_publishers``, 0154). The hub accepts the union of both
(``service.store.trusted_publishers``).

- GET /v1/plugin-publishers: every publisher, the operator's first in their order, each with what signs for it
  (``identities``, the operator's and the trusted ones), whether its plugins may be core plugins, and the signers hub
  admins trusted for it (``trusted``), with who and when;
- POST /v1/plugin-publishers: trust a signer for a publisher;
- DELETE /v1/plugin-publishers/{id}: stop trusting one; the row stays with who removed it and when.

All three are for hub admins. EVO_HUB_PLUGIN_PUBLISHERS_LOCKED=true refuses a write (409, with the reason), and a
core publisher (EVO_HUB_PLUGIN_CORE_PUBLISHERS) takes no signer from here: its signers run Python in the hub's
processes, so only the operator names them. Writes are audited as plugin.publisher.add and plugin.publisher.remove,
naming ``publisher:<id> signer:<signer>``. ``evo-agents hub plugin publisher list|add|remove`` is the client.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugins.models.publishers import PluginPublisher, PublisherTrustIn
from evo_agents.hub.server.plugins.service.publishers import list_publishers, remove, trust
from evo_agents.hub.server.security import AdminUser, admin

router = APIRouter(
    prefix="/v1/plugin-publishers",
    tags=["plugins"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


@router.get("", response_model=list[PluginPublisher])
async def publishers(request: Request) -> list[PluginPublisher]:
    """The publishers this hub trusts: the operator's in the order EVO_HUB_PLUGIN_PUBLISHERS names them, then those
    only hub admins trusted."""
    async with request.app.state.engine.begin() as conn:
        return await list_publishers(conn, request.app.state.config)


@router.post("", response_model=list[PluginPublisher], responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}})
async def add_publisher(request: Request, body: PublisherTrustIn, user: AdminUser) -> list[PluginPublisher]:
    """Trust ``signer`` to sign the plugins of ``publisher``; nothing changes when the hub trusts it already."""
    config = request.app.state.config
    async with request.app.state.engine.begin() as conn:
        await trust(conn, user, config, body.publisher, body.signer, body.note)
        return await list_publishers(conn, config)


@router.delete("/{entry_id}", response_model=list[PluginPublisher], responses={404: {"model": ErrorBody}})
async def remove_publisher(
    request: Request, entry_id: Annotated[int, Path(ge=1)], user: AdminUser
) -> list[PluginPublisher]:
    """Stop trusting a signer hub admins trusted; a plugin it signed stays installed, and installing or upgrading
    it again is refused until something the hub trusts signs it."""
    config = request.app.state.config
    async with request.app.state.engine.begin() as conn:
        await remove(conn, user, entry_id)
        return await list_publishers(conn, config)
