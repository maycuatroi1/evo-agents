# The plugin market

A market is a GitHub repository holding `market.json`, an index of plugins in the format plugin-market-v1
(`evo_agents/harness/schemas/plugin-market.schema.json`). The index names each plugin's GitHub repository, its
publisher and the signer of its releases, its tier and its price. A hub reads the markets its admins add, and every
member browses what they list. A hub admin installs a free isolated plugin from its GitHub release, and the hub checks
the release against the publisher the market names before anything runs. The bundles themselves stay in the plugins'
own repositories, as release assets: the market holds no code.

The market takes no payment itself: a paid plugin is bought from its seller, another hub, by bank transfer (section
Selling plugins). It adds no install counts or telemetry, and no reviews or ratings. A core plugin is never
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
  seller one of `sellers`. A seller's `url` is the address of the hub that sells the plugin, and `keys` the key id and
  public key of each key that signs its licences. A paid plugin shows on the hub as needing a licence until a hub
  admin buys it; it then installs from its seller, not from a GitHub release (section Selling plugins).
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

The admin of a self-managed project installs from a GitHub release too, without a hub admin, when the repository's
owner is one the hub admin trusts for that project: `hub plugin install --github OWNER/REPO --project PROJECT` (see
`docs/hub.md`, section A self-managed project). The signer must be a workflow of that repository, it is trusted for that
release alone, and the plugin is that project's alone.

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

## Selling plugins

A hub sells plugins with the feature `seller`, off by default (`docs/hub.md`, section Branding and features). It needs a seller
id, `EVO_HUB_SELLER_ID`, the `sellers[].id` the markets list it under, and the Ed25519 key that signs its licences,
`EVO_HUB_SELLER_KEY`. `hub market seller-key` writes a new key to a file only its owner reads and prints the key id
and public key to put in the market's index; the private key is a secret, which the hub never prints or returns.
`EVO_HUB_SELLER_PAYMENT_INSTRUCTIONS` is what a buyer is told to pay by, such as the seller's bank account.

```sh
evo-agents hub market seller-key ./seller.key
```

The index lists the seller with the address of its hub and its keys:

```json
"sellers": [
  {"id": "northwind", "url": "https://hub.northwind.example", "keys": [{"key_id": "ed25519:...", "public_key": "..."}]}
]
```

### Listing a plugin

A publisher signed in with GitHub lists a plugin at a price in USD per hub per month, then uploads its signed bundle,
which the hub checks against the signer the listing names. Unlike a free plugin, a paid bundle is not a public release:
the seller's hub keeps it and serves it only to a hub that holds a licence of it.

```sh
evo-agents hub market listing add northwind-ledger --publisher northwind --signer ed25519:KEY --usd 49.50
evo-agents hub market bundle upload northwind-ledger northwind-ledger-1.0.0.tar.gz --signature northwind-ledger-1.0.0.tar.gz.sig.json
evo-agents hub market listing set northwind-ledger --usd 59
```

### Buying

A hub admin clicks Buy on the plugin's page, or runs `hub market buy`, and gives the months the licence covers and a
name and email to invoice. The order goes to the seller's hub with this hub's stable id (`hub market hub-id`) and
its address; the hub shows the order number and how to pay it, by bank transfer with the order number as the
reference. Buying needs `EVO_HUB_SECRETS_KEY`, which seals the order's secret. Buying from a publisher the hub does
not trust yet trusts its signer, as installing does.

```sh
evo-agents hub market buy northwind-ledger --months 12 --name "Atlas Ltd" --email ops@atlas.example
evo-agents hub market licenses
```

Once the money arrives, a hub admin of the seller marks the order paid with the invoice number. That issues the
licence, an Ed25519 signature over the plugin, the buying hub's id and the days it covers.

```sh
evo-agents hub market orders --state open
evo-agents hub market paid ORD-2026-0001 --invoice INV-0042
```

Every hour, and at once with Check now on `/admin/market` or `hub market licenses --check`, the buying hub asks its
sellers about the open orders. It checks a licence it receives offline, with the keys the market's index lists for the
seller, then downloads the bundle, which the seller serves only for a valid licence of that exact hub, checks its
signature and installs it.

### Expiry and renewal

Seven days before a licence ends, the hub admins get a notice in the Inbox, `market_license_expiring`. Seven days
after it ends, the hub turns the plugin off everywhere, with its config kept, and says so in the Inbox. Buying it
again renews it: the new licence starts where the last one ends, or today when it lapsed, and the next check turns the
plugin on again. A plugin whose licence does not cover today cannot be installed from the store either.

### Revenue and payouts

`hub market revenue --month YYYY-MM`, and the Selling page under `/admin/market/seller`, total the orders marked paid
in a month by publisher: 80% to the publisher, 20% to the platform, to the cent. The hub pays nobody itself. Once a
month is over and a publisher's share is paid, a hub admin marks it paid out, once per publisher and month.

```sh
evo-agents hub market revenue --month 2026-09
evo-agents hub market payout --month 2026-09 --publisher northwind --note "bank transfer 2026-10-02"
```

When `EVO_HUB_SELLER_SIGNAL_PROJECT` names a project, the seller writes the signals `market.active_licenses` and
`market.revenue_usd_month` into it after each payment and every day. Every order, payment, cancellation, licence and
payout is in the audit log. Payment goes through a payment-provider interface whose only provider is the bank
transfer; there is no payment gateway, no automatic payout and no tax.
