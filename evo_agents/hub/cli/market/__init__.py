"""``evo-agents hub market``: the plugin markets the hub reads (``evo_agents.hub.server.market``), and installing
from them or from a GitHub release (``evo_agents.hub.server.market_install``); docs/market.md.

- ``market list [--search WORDS] [--industry I] [--category C] [--source ID]`` prints the plugins of every market with
  their status on the hub; ``market show NAME [--source ID]`` one plugin with its publisher and signer, what its latest
  release asks for, its price and whether the hub trusts its signer;
- ``market install NAME [--source ID] [--version V] [--trust]`` (hub admins) installs or upgrades a free plugin from
  its GitHub release; a signer the hub does not trust yet is asked about at a terminal, or trusted with ``--trust``;
- ``market request NAME --project P [--source ID]`` (a project's admins) asks the hub admins to install one;
- ``market source list|add REPO [--ref] [--path] [--token-stdin]|remove ID|refresh ID`` (hub admins): the markets;
- buying a paid plugin (``evo_agents.hub.server.market_licenses``): ``market buy NAME --months N --name --email
  [--trust]`` orders it from its seller and prints how to pay; ``market licenses [--check]`` the hub's orders and
  licences; ``market hub-id`` the id a licence names;
- selling them, the hub feature ``seller`` (``evo_agents.hub.server.seller``): ``market seller-key FILE`` writes a
  new key and prints only its public half, ``market seller``, ``market listing list|add|set``, ``market bundle
  upload``, ``market orders [--state]``, ``market paid NUMBER --invoice``, ``market cancel NUMBER``, ``market revenue
  [--month YYYY-MM]`` and ``market payout --month --publisher [--note]``.

``evo-agents hub plugin install --github OWNER/REPO[@TAG] [--signer S] [--trust]`` runs :func:`install_github`, and
``hub plugin publisher list|add|remove`` is the group :data:`PUBLISHER`; ``evo_agents.hub.cli.plugin`` takes both from
here and hangs them under ``hub plugin``.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes), one module per seam:
``catalog`` (list, show, install, request and the GitHub install), ``sources``, ``buying`` (buy, licenses, hub-id),
``seller`` (seller-key, seller, listing, bundle), ``orders`` (orders, paid, cancel, revenue, payout) and
``publishers``; ``resources`` holds the gateways to the hub's routes and the keys of their answers. Standard library
only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Group
from evo_agents.hub.cli.market.buying import MARKET_BUY, MARKET_HUB_ID, MARKET_LICENSES
from evo_agents.hub.cli.market.catalog import (
    MARKET_INSTALL,
    MARKET_LIST,
    MARKET_REQUEST,
    MARKET_SHOW,
    install_github,
)
from evo_agents.hub.cli.market.orders import MARKET_CANCEL, MARKET_ORDERS, MARKET_PAID, MARKET_PAYOUT, MARKET_REVENUE
from evo_agents.hub.cli.market.publishers import PUBLISHER
from evo_agents.hub.cli.market.resources import INSTALLED_KEYS
from evo_agents.hub.cli.market.seller import (
    MARKET_BUNDLE,
    MARKET_LISTING,
    MARKET_SELLER,
    MARKET_SELLER_KEY,
    seller_key_json,
)
from evo_agents.hub.cli.market.sources import MARKET_SOURCE

__all__ = ["INSTALLED_KEYS", "MARKET", "PUBLISHER", "install_github", "seller_key_json"]

MARKET = Group(
    "market",
    "the plugin markets: browse them, install from them, add them",
    (
        MARKET_LIST,
        MARKET_SHOW,
        MARKET_INSTALL,
        MARKET_REQUEST,
        MARKET_SOURCE,
        MARKET_BUY,
        MARKET_LICENSES,
        MARKET_HUB_ID,
        MARKET_SELLER_KEY,
        MARKET_SELLER,
        MARKET_LISTING,
        MARKET_BUNDLE,
        MARKET_ORDERS,
        MARKET_PAID,
        MARKET_CANCEL,
        MARKET_REVENUE,
        MARKET_PAYOUT,
    ),
)
