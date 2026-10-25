# The plugin market

A market is a GitHub repository holding `market.json`, an index of plugins in the format plugin-market-v1
(`evo_agents/harness/schemas/plugin-market.schema.json`). The index names each plugin's GitHub repository, its
publisher and the signer of its releases, its tier and its price. A hub reads the markets its admins add, and every
member browses what they list. A hub admin installs a free isolated plugin from its GitHub release, and the hub checks
the release against the publisher the market names before anything runs. The bundles themselves stay in the plugins'
own repositories, as release assets: the market holds no code.

The market adds no payment, no install counts or telemetry, and no reviews or ratings. A core plugin is never
installed from a market: it ships in the hub's image (`docs/plugins.md`, section Core plugins).

## The index

```json
{
  "schema": "plugin-market-v1",
  "name": "northwind-market",
  "title": "Northwind market",
  "owner": {"name": "Northwind", "url": "https://github.com/northwind"},
  "sellers": [
    {"id": "northwind", "url": "https://licenses.northwind.test", "keys": [{"key_id": "...", "public_key": "..."}]}
  ],
  "plugins": [
    {
      "id": "lumen-notes",
      "name": "Lumen notes",
      "description": "Meeting notes for a project, with action items as tasks.",
      "industries": ["education"],
      "categories": ["documents"],
      "repo": "lumen-labs/lumen-notes",
      "publisher": {"id": "lumen", "name": "Lumen Labs", "signer": "sigstore:lumen-labs/lumen-notes/.github/workflows/ci.yml"},
      "tier": "isolated",
      "price": {"kind": "free"},
      "homepage": "https://github.com/lumen-labs/lumen-notes",
      "license": "Apache-2.0"
    }
  ]
}
```

- `publisher.signer` is what signs the plugin's releases: `sigstore:OWNER/REPO/.github/workflows/FILE.yml`, a GitHub
  Actions workflow that signs keyless, or `ed25519:KEY`, a public key in base64. It reads as an entry of
  `EVO_HUB_PLUGIN_PUBLISHERS` does (`docs/hub.md`, section Plugins).
- `tier` is `isolated` or `core`. A core plugin names `package`, the Python package that ships it in an image, and is
  never sold by subscription.
- `price` is `{"kind": "free"}` or `{"kind": "subscription", "usd_per_hub_month": 49.5, "seller": "northwind"}`, the
  seller one of `sellers`, whose keys will sign its licenses. A paid plugin shows on the hub as needing a license and
  installs from nowhere yet.
- An index holds at most 2000 plugins and 50 sellers, in at most 1 MiB.

A release of a plugin is tagged `vX.Y.Z` and carries the bundle `evo-agents plugin pack` writes, `ID-X.Y.Z.tar.gz`,
and its signature beside it, `ID-X.Y.Z.tar.gz.sigstore.json` (Sigstore) or `.sig.json` (Ed25519). The sign job of the
plugin template's CI writes both (`docs/plugins.md`, section Signing in CI). Before publishing an index, its owner
checks it, and with `--releases` the latest release of each free plugin too:

```sh
evo-agents plugin market-check market.json
evo-agents plugin market-check market.json --releases
```

## Markets on a hub

The operator's market is there from the start: `EVO_HUB_MARKET_DEFAULT_SOURCE` names it, `maycuatroi1/evo-agents-market`
unless set, and `none` means none. A hub admin adds more, from Administration, Market or from the command line.
The hub reads each market's index when it is added, then every six hours. Each read also reads the latest release of
each free isolated plugin, so the market shows its version and the permissions it asks for. A read that fails keeps
the last index that read and shows why beside the market. For a private repository, the admin gives a read-only GitHub
token, which the hub seals and never shows again.

```sh
evo-agents hub market source list
evo-agents hub market source add northwind/market --ref main --path market.json
printf '%s' "$TOKEN" | evo-agents hub market source add northwind/private-market --token-stdin
evo-agents hub market source refresh 2
evo-agents hub market source remove 2
```

The operator's market cannot be removed; removing another one leaves the plugins installed from it installed.

## Browsing

Every member reads the market on the web, under Market (`/market`), or from the command line. The list searches the
id, name, description and publisher, and filters by industry and by category. A plugin's page shows its publisher and
the identity that signs its releases, whether this hub trusts it, its latest release and the version installed here,
the permissions the release asks for, its price and its status:

| status | means |
| --- | --- |
| `not_installed` | a free isolated plugin this hub can install |
| `installed` | installed here from this publisher |
| `upgrade` | installed, and a newer release is out |
| `in_image` | a core plugin in this hub's image: a project's admin enables it from the project's Plugins |
| `not_in_image` | a core plugin this hub's image lacks |
| `needs_license` | a paid plugin |
| `unavailable` | its latest release does not read, or it has none |

```sh
evo-agents hub market list --search notes --industry education
evo-agents hub market show lumen-notes
```

## Installing

A hub admin installs or upgrades a free isolated plugin from its page, or with `hub market install`. The hub downloads
the release's bundle and its signature from GitHub. It checks that the bundle is the market's plugin at the release's
version, by the market's publisher, signed by the signer the market names. It then publishes and installs it through
the store, as `hub plugin publish` and `hub plugin install` would (`docs/hub.md`, section Plugins).

When the hub does not trust that signer for the publisher yet, it asks first. The web shows a dialog naming the
publisher and the signer. On the command line, `--trust` says yes, and at a terminal the command asks. Saying yes
stores the signer in the hub's database (table `plugin_publishers`) beside the publishers the operator lists, and
records who trusted it. From then on, a new release signed by it upgrades without asking. A hub admin stops trusting
it from Administration, Market, or with `hub plugin publisher remove`. Its installed plugins keep running, but its next
version does not install until someone trusts the signer again. The publishers the operator lists in
`EVO_HUB_PLUGIN_PUBLISHERS` are not stored and cannot be removed this way. A core publisher's signer is never trusted
from the market.

The operator locks the list with `EVO_HUB_PLUGIN_PUBLISHERS_LOCKED=true`. The hub then refuses to trust a new signer,
whatever the web or the command line asks, and installs from a market only what a trusted signer signed.

A hub admin also installs a plugin straight from a GitHub repository's release, without any market. The signer
defaults to the repository's `.github/workflows/ci.yml`, as the plugin template signs:

```sh
evo-agents hub market install lumen-notes --trust
evo-agents hub plugin install --github lumen-labs/lumen-notes@v1.2.0 --trust
evo-agents hub plugin install --github lumen-labs/lumen-notes --signer ed25519:KEY
evo-agents hub plugin publisher list
evo-agents hub plugin publisher remove 4
```

A refusal says why, and installs nothing:
- 409 for a signer the hub does not trust and was not asked to trust, a locked list, a core or paid plugin, or a
  release older than the installed version;
- 422 for a release that does not read (no version tag, no bundle, no signature, a bundle of another plugin or
  version, a signature that does not verify);
- 502 for GitHub answering wrong, 503 for a signature the hub cannot check now.

Each install is audited as the store's are (`plugin.publish`, `plugin.install`, `plugin.upgrade`), each trust as
`plugin.publisher.add` and each stop as `plugin.publisher.remove`; a market added or removed as `market.source.add` or
`market.source.remove`, and a request as `market.request`.

## Asking for a plugin

A project's admin who is no hub admin asks for a plugin from its page, or with `hub market request`. Every hub admin
gets a notice in the Inbox, `market_install_request`, naming the project and linking to the plugin. Asking again
while the request is open asks nobody twice. Installing the plugin closes the open requests for it. The project's
admin then enables the plugin in the project, granting what it asks for (`docs/plugins.md`, section Permissions).

```sh
evo-agents hub market request lumen-notes --project atlas
```
