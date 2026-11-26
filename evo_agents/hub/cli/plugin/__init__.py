"""``evo-agents hub plugin publish|list|show|install|upgrade|remove``: the hub's store of plugins
(``evo_agents.hub.server.plugin_store``), for hub admins.

``publish BUNDLE`` reads the bundle here first (``evo_agents.plugins.bundle``: a tar.gz with evo-plugin.json at its
top), then uploads it to the blob store as the hub's own blob, commits it and asks the hub to publish it as the version
its manifest names, with its signature: ``--signature FILE``, else ``BUNDLE.sig.json`` or ``BUNDLE.sigstore.json``
beside it. The file is an envelope of ``evo_agents.plugins.signing`` or the Sigstore bundle ``sigstore sign`` wrote;
without one the hub refuses the bundle as unsigned. The hub checks the bundle whole and says why it refuses it.

``list`` prints every plugin with its state, installed and latest version and the upgrade waiting; ``show NAME`` one
plugin with every version. ``install NAME`` installs the newest version that runs on the hub, or ``--version``;
``install --github OWNER/REPO[@TAG]`` installs from a GitHub release instead (``evo_agents.hub.cli.market``);
``upgrade NAME`` moves the installed one to the newest above it, or ``--version``; ``remove NAME`` removes it, every
version staying in the store. ``--json`` prints what the hub answered. ``publisher list|add|remove`` are in
``evo_agents.hub.cli.market``. ``list --project``, ``enable``, ``disable``, ``config`` and ``rollback``, for the
members of a project, are in ``evo_agents.hub.cli.plugin.projects``, ``agent-type`` in ``cli.plugin.agent_types``.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`PluginStore`
is the gateway to the hub's store (/v1/plugins) and :class:`Blobs` to its blob store, and :data:`PLUGIN` the group
``evo_agents.hub.cli`` attaches, its children listed by hand. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from urllib.parse import quote

from evo_agents.clikit import Arg, Group, Result, Table, Text
from evo_agents.hub.cli.base import EXIT_FAILED, HubCommand, HubContext, when
from evo_agents.hub.cli.market import INSTALLED_KEYS, PUBLISHER, install_github
from evo_agents.hub.cli.plugin.agent_types import AGENT_TYPE
from evo_agents.hub.cli.plugin.projects import (
    CONFIG,
    DISABLE,
    ENABLE,
    NAME,
    PROJECT_LIST,
    ROLLBACK,
    ProjectPlugins,
    project_list,
)
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.contract import JsonOutput, returns_object
from evo_agents.hub.skill_sync import put_bundle
from evo_agents.plugins.bundle import KIND, MAX_BUNDLE, BundleError, read_bundle
from evo_agents.plugins.signing import SignatureError, read_signature, sha256_of

# The keys of the hub's answers with --json (the contract, `evo-agents hub contract print`).
PLUGIN_KEYS = (
    "name",
    "publisher",
    "state",
    "installed_version",
    "installed_by",
    "installed_at",
    "disabled_reason",
    "disabled_at",
    "latest_version",
    "upgrade_to",
    "project",
    "created_at",
    "title",
    "description",
    "tier",
)
DETAIL_KEYS = (*PLUGIN_KEYS, "manifest", "versions")
PUBLISHED_KEYS = ("name", "created", "version")
SIGNATURE_SUFFIXES = (".sig.json", ".sigstore.json")
VERSION = Arg("--version", metavar="VERSION", help="the version, such as 1.2.0")


# The gateways


class PluginStore:
    """The hub's store of plugins: GET /v1/plugins and /v1/plugins/{name}, POST /v1/plugins/{name}/{action} (Gateway)."""

    def __init__(self, hub: Hub):
        self.hub = hub

    def path(self, name: str | None = None, action: str | None = None) -> str:
        path = "/v1/plugins"
        if name is not None:
            path += f"/{quote(name, safe='')}"
        return path if action is None else f"{path}/{action}"

    def list(self) -> Any:
        return self.hub.call("GET", self.path())

    def get(self, name: str) -> Any:
        return self.hub.call("GET", self.path(name))

    def act(self, name: str, action: str, body: dict | None = None) -> Any:
        """POST /v1/plugins/{name}/{action}: install, upgrade, remove, or versions to publish one."""
        return self.hub.call("POST", self.path(name, action), body)


class Blobs:
    """The hub's blob store: POST /v1/blobs/uploads and /v1/blobs/commit (Gateway)."""

    def __init__(self, hub: Hub):
        self.hub = hub

    def uploads(self, sha256: str, size: int) -> list[dict]:
        """The upload tickets of one bundle: none when the hub holds it already; HubError when the hub did not answer
        with this bundle."""
        asked = self.hub.call("POST", "/v1/blobs/uploads", {"items": [{"sha256": sha256, "size": size, "kind": KIND}]})
        uploads = asked.get("uploads") if isinstance(asked, dict) else None
        if not isinstance(uploads, list) or any(not isinstance(u, dict) or u.get("sha256") != sha256 for u in uploads):
            raise HubError(f"the hub at {self.hub.url} did not answer the upload request with this bundle")
        return uploads

    def commit(self, upload_id: Any) -> None:
        self.hub.call("POST", "/v1/blobs/commit", {"upload_ids": [upload_id]})


# Publishing


def signature_for(bundle: Path, given: str | None) -> dict | None:
    """The signature envelope of ``bundle``: from ``given``, else from a file beside it; None when there is none."""
    candidates = [Path(given)] if given else [Path(f"{bundle}{suffix}") for suffix in SIGNATURE_SUFFIXES]
    for path in candidates:
        if not path.is_file():
            if given:
                raise HubError(f"{path} is not a file")
            continue
        try:
            return read_signature(path.read_bytes())
        except SignatureError as exc:
            raise HubError(f"{path}: {exc}") from None
    return None


def read_bundle_file(path: Path) -> bytes:
    """The bytes of the bundle at ``path``; HubError when it cannot be read or is over the size a bundle may have."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HubError(f"cannot read {path}: {exc.strerror or exc}") from None
    if size > MAX_BUNDLE:
        raise HubError(f"{path} is {size} bytes, over the {MAX_BUNDLE // (1024 * 1024)} MiB a plugin bundle may have")
    return path.read_bytes()


def publish(hub: Hub, bundle_path: str, signature_path: str | None = None) -> dict:
    """Upload, commit and publish the bundle at ``bundle_path``; the hub's answer."""
    path = Path(bundle_path).expanduser()
    data = read_bundle_file(path)
    try:
        manifest = read_bundle(data).manifest
    except BundleError as exc:
        raise HubError(f"{path} is not a plugin bundle: {exc}") from None
    signature = signature_for(path, signature_path)
    sha256 = sha256_of(data)
    blobs = Blobs(hub)
    for ticket in blobs.uploads(sha256, len(data)):
        put_bundle(ticket["url"], data)
        blobs.commit(ticket["upload_id"])
    body = {"sha256": sha256, "size": len(data), "signature": signature}
    return PluginStore(hub).act(manifest.id, "versions", body)


def published_lines(published: dict) -> list[str]:
    version = published["version"]
    verb = "Published" if published["created"] else "The hub holds already"
    lines = [f"{verb} plugin {published['name']} {version['version']}, signed by {version['signed_by']}."]
    if not version["compatible"]:
        lines.append(f"  It does not run on this hub: {version['incompatible_reason']}")
    return lines


def plugin_lines(plugin: dict) -> list[str]:
    """``hub plugin show``: the plugin, its state and every version with its signer and what it asks for."""
    installed = plugin["installed_version"]
    lines = [f"Plugin {plugin['name']} of publisher {plugin['publisher']}: {plugin['state']}"]
    if installed:
        by = plugin["installed_by"] or "the hub's image"  # nobody installs a core plugin of the image
        lines.append(f"  installed {installed} by {by} at {when(plugin['installed_at'])} UTC")
    if plugin["disabled_reason"]:
        lines.append(f"  turned off at {when(plugin['disabled_at'])} UTC: {plugin['disabled_reason']}")
    if plugin["upgrade_to"]:
        lines.append(f"  upgrade waiting: {plugin['upgrade_to']}")
    for version in plugin["versions"]:
        runs = "runs here" if version["compatible"] else f"does not run here: {version['incompatible_reason']}"
        lines.append(
            f"  {version['version']}: {version['tier']}, evo-agents {version['engines']}, signed by "
            f"{version['signed_by']} ({version['signature_kind']}), {runs}"
        )
        if version["permissions"]:
            lines.append(f"    asks for {', '.join(version['permissions'])}")
    return lines


def changed(plugin: dict, verb: str) -> Result:
    """What install, upgrade and remove print of the plugin the hub answered with."""
    if plugin["installed_version"]:
        return Result(plugin, Text(f"{verb} plugin {plugin['name']} {plugin['installed_version']}."))
    return Result(plugin, Text(f"{verb} plugin {plugin['name']}; its versions stay on the hub."))


# The commands


class Publish(HubCommand):
    name = "publish"
    help = "upload a signed plugin bundle and publish it as its version"
    arguments = (
        Arg("bundle", metavar="BUNDLE", help="the plugin's tar.gz, evo-plugin.json at its top"),
        Arg("--signature", metavar="FILE", help="its signature (default: BUNDLE.sig.json or BUNDLE.sigstore.json)"),
    )
    output = returns_object(*PUBLISHED_KEYS, schema="PluginPublished")

    def run(self, ctx: HubContext) -> Result:
        published = publish(ctx.hub, ctx.args.bundle, ctx.args.signature)
        return Result(published, Text(*published_lines(published)))


class ListPlugins(HubCommand):
    name = "list"
    help = "every plugin of the hub: state, installed and latest version"
    arguments = (Arg("--project", metavar="PROJECT", help="the installed plugins as the project has them"),)
    output = JsonOutput("array", PLUGIN_KEYS, "Plugin", variants=(("--project", PROJECT_LIST),))
    columns = ("name", "state", "installed_version", "latest_version", "upgrade_to", "publisher")

    def run(self, ctx: HubContext) -> Result:
        if ctx.args.project:
            return project_list(ctx)
        found = PluginStore(ctx.hub).list()
        rows = [tuple(p[column] or "-" for column in self.columns) for p in found]
        empty = "The hub holds no plugin: publish one with `evo-agents hub plugin publish BUNDLE`."
        return Result(found, Table(("NAME", "STATE", "INSTALLED", "LATEST", "UPGRADE", "PUBLISHER"), rows, empty))


class Show(HubCommand):
    name = "show"
    help = "one plugin with every version, its signer and what it asks for"
    arguments = (NAME,)
    output = returns_object(*DETAIL_KEYS, schema="PluginDetail")

    def run(self, ctx: HubContext) -> Result:
        plugin = PluginStore(ctx.hub).get(ctx.args.name)
        return Result(plugin, Text(*plugin_lines(plugin)))


class Install(HubCommand):
    """``install NAME`` from the hub's store; ``install --github`` runs ``install_github`` of
    ``evo_agents.hub.cli.market`` in its place."""

    name = "install"
    help = "install a version: the newest that runs on the hub, or --version; or a GitHub release"
    arguments = (
        Arg("name", metavar="NAME", nargs="?", help="the plugin's id, as its manifest gives it"),
        VERSION,
        Arg("--github", metavar="OWNER/REPO[@TAG]", help="install from the repo's GitHub release: the latest, or TAG"),
        Arg(
            "--signer",
            help="with --github: what signs its releases (default: sigstore:OWNER/REPO/.github/workflows/ci.yml)",
        ),
        Arg("--trust", action="store_true", help="with --github: trust the signer if the hub does not yet"),
        Arg(
            "--project",
            metavar="PROJECT",
            help="with --github: as an admin of a self-managed project, install for it alone from a trusted owner",
        ),
    )
    output = returns_object(
        *PLUGIN_KEYS,
        schema="Plugin",
        variants=(("--github", returns_object(*INSTALLED_KEYS, schema="MarketInstalled")),),
    )

    def check(self, args: argparse.Namespace) -> None:
        if (args.github is None) == (args.name is None):
            raise HubError("name the plugin, or its GitHub repo with --github OWNER/REPO[@TAG], not both")
        if args.github is not None:
            if args.project and args.trust:
                raise HubError("--trust is for a hub admin: a project's admin trusts the signer of this release alone")
        elif args.signer or args.trust or args.project:
            raise HubError("--signer, --trust and --project go with --github")

    def __call__(self, args: argparse.Namespace) -> int:
        if args.github is None or args.name is not None:
            return super().__call__(args)
        try:
            self.check(args)
            return install_github(args)
        except HubError as exc:
            status = self.failed(exc)
            return EXIT_FAILED if status is None else status

    def run(self, ctx: HubContext) -> Result:
        return changed(PluginStore(ctx.hub).act(ctx.args.name, "install", {"version": ctx.args.version}), "Installed")


class Upgrade(HubCommand):
    name = "upgrade"
    help = "move the installed version to a newer one: the newest that runs, or --version"
    arguments = (NAME, VERSION)
    output = returns_object(*PLUGIN_KEYS, schema="Plugin")

    def run(self, ctx: HubContext) -> Result:
        return changed(PluginStore(ctx.hub).act(ctx.args.name, "upgrade", {"version": ctx.args.version}), "Upgraded")


class Remove(HubCommand):
    name = "remove"
    help = "remove the installed version; every version stays on the hub"
    arguments = (NAME, Arg("--project", metavar="PROJECT", help="remove a plugin this self-managed project installed"))
    output = returns_object(*PLUGIN_KEYS, schema="Plugin")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        if args.project:  # a plugin a self-managed project installed
            return changed(ProjectPlugins(ctx.hub, args.project).act(args.name, "remove"), "Removed")
        return changed(PluginStore(ctx.hub).act(args.name, "remove"), "Removed")


PLUGIN = Group(
    "plugin",
    "the hub's plugins: publish and install them (hub admins), enable and configure them per project",
    [
        Publish(),
        ListPlugins(),
        Show(),
        Install(),
        Upgrade(),
        Remove(),
        ENABLE,
        DISABLE,
        CONFIG,
        ROLLBACK,
        AGENT_TYPE,
        PUBLISHER,
    ],
)
