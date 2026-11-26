"""``evo-agents plugin test|pack|sign|keygen|market-check``: what the author of a plugin, or of a market, runs on
their own machine. Each command is a class of ``evo_agents.clikit`` in the module named for its word (``test`` in
``conformance``), and :func:`tree` builds the ``plugin`` group of them.

- ``test DIR``: the conformance checks of ``evo_agents.plugins.conformance``; exit 1 when one fails;
- ``pack DIR``: the plugin's deterministic bundle, ``<id>-<version>.tar.gz`` in the current directory or ``--output``,
  and its SHA-256;
- ``sign BUNDLE --key FILE``: an Ed25519 signature of the bundle, written beside it as ``BUNDLE.sig.json``, which
  ``evo-agents hub plugin publish`` picks up. A bundle a GitHub Actions workflow signs with Sigstore needs no command
  here: ``python -m sigstore sign BUNDLE`` writes ``BUNDLE.sigstore.json``;
- ``keygen --output FILE``: a new Ed25519 key, its 32-byte seed in base64 in FILE (mode 0600), and the
  EVO_HUB_PLUGIN_PUBLISHERS entry of its public key;
- ``market-check FILE [--releases]``: the index of a market, ``market.json``, against plugin-market-v1
  (``evo_agents.plugins.market``); with ``--releases``, the latest GitHub release of each free isolated plugin it
  lists carries the bundle and its signature, which the identity the index names signed. It reads GitHub at
  ``GITHUB_API_URL`` (default https://api.github.com) with ``GITHUB_TOKEN`` when set; exit 1 on any problem.

Signing imports ``cryptography``, which the hub-server extra brings. Standard library otherwise.
"""

from __future__ import annotations

from evo_agents.clikit import Group


def tree() -> Group:
    """The ``plugin`` command."""
    from evo_agents.plugins.cli.conformance import TEST
    from evo_agents.plugins.cli.keygen import KEYGEN
    from evo_agents.plugins.cli.market_check import MARKET_CHECK
    from evo_agents.plugins.cli.pack import PACK
    from evo_agents.plugins.cli.sign import SIGN

    return Group(
        "plugin",
        "test, pack and sign a plugin on this machine",
        (TEST, PACK, SIGN, KEYGEN, MARKET_CHECK),
        dest="command",
    )


def register(sub) -> None:
    tree().attach(sub)
