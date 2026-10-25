"""``evo-agents hub plugin publish|list|show|install|upgrade|remove``: the hub's store of plugins
(``evo_agents.hub.server.plugin_store``), for hub admins.

``publish BUNDLE`` reads the bundle here first (``evo_agents.plugins.bundle``: a tar.gz with evo-plugin.json at its
top), then uploads it to the blob store as the hub's own blob, commits it and asks the hub to publish it as the version
its manifest names, with its signature: ``--signature FILE``, else ``BUNDLE.sig.json`` or ``BUNDLE.sigstore.json``
beside it. The file is an envelope of ``evo_agents.plugins.signing`` or the Sigstore bundle ``sigstore sign`` wrote;
without one the hub refuses the bundle as unsigned. The hub checks the bundle whole and says why it refuses it.

``list`` prints every plugin with its state, installed and latest version and the upgrade waiting; ``show NAME`` one
plugin with every version. ``install NAME`` installs the newest version that runs on the hub, or ``--version``;
``install --github OWNER/REPO[@TAG]`` installs from a GitHub release instead (``evo_agents.hub.cli_market``);
``upgrade NAME`` moves the installed one to the newest above it, or ``--version``; ``remove NAME`` removes it, every
version staying in the store. ``--json`` prints what the hub answered. Standard library only, like the rest of the
client. ``publisher list|add|remove`` are in ``evo_agents.hub.cli_market``. ``list --project``, ``enable``, ``disable``, ``config`` and ``rollback``, for the members of a project, are in
``evo_agents.hub.cli_plugin_projects``.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.cli_market import INSTALLED_KEYS, install_github, register_publishers
from evo_agents.hub.cli_plugin_projects import list_project, project_list_output, register_project_commands
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import JsonOutput, json_option, returns_array, returns_object
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
    "created_at",
    "title",
    "description",
    "tier",
)
DETAIL_KEYS = (*PLUGIN_KEYS, "manifest", "versions")
PUBLISHED_KEYS = ("name", "created", "version")
SIGNATURE_SUFFIXES = (".sig.json", ".sigstore.json")


def _path(name: str | None = None, action: str | None = None) -> str:
    path = "/v1/plugins"
    if name is not None:
        path += f"/{quote(name, safe='')}"
    return path if action is None else f"{path}/{action}"


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


def _read(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HubError(f"cannot read {path}: {exc.strerror or exc}") from None
    if size > MAX_BUNDLE:
        raise HubError(f"{path} is {size} bytes, over the {MAX_BUNDLE // (1024 * 1024)} MiB a plugin bundle may have")
    return path.read_bytes()


def publish(hub, bundle_path: str, signature_path: str | None = None) -> dict:
    """Upload, commit and publish the bundle at ``bundle_path``; the hub's answer."""
    path = Path(bundle_path).expanduser()
    data = _read(path)
    try:
        manifest = read_bundle(data).manifest
    except BundleError as exc:
        raise HubError(f"{path} is not a plugin bundle: {exc}") from None
    signature = signature_for(path, signature_path)
    sha256 = sha256_of(data)
    asked = hub.call("POST", "/v1/blobs/uploads", {"items": [{"sha256": sha256, "size": len(data), "kind": KIND}]})
    uploads = asked.get("uploads") if isinstance(asked, dict) else None
    if not isinstance(uploads, list) or any(not isinstance(u, dict) or u.get("sha256") != sha256 for u in uploads):
        raise HubError(f"the hub at {hub.url} did not answer the upload request with this bundle")
    for ticket in uploads:  # none when the hub holds the bundle already
        put_bundle(ticket.get("url"), data)
        hub.call("POST", "/v1/blobs/commit", {"upload_ids": [ticket["upload_id"]]})
    body = {"sha256": sha256, "size": len(data), "signature": signature}
    return hub.call("POST", _path(manifest.id, "versions"), body)


@client_command
def cmd_publish(args) -> int:
    hub, _ = signed_in()
    published = publish(hub, args.bundle, args.signature)
    if args.json:
        print_json(published)
        return 0
    version = published["version"]
    verb = "Published" if published["created"] else "The hub holds already"
    print(f"{verb} plugin {published['name']} {version['version']}, signed by {version['signed_by']}.")
    if not version["compatible"]:
        print(f"  It does not run on this hub: {version['incompatible_reason']}")
    return 0


@client_command
def cmd_list(args) -> int:
    if args.project:
        return list_project(args)
    hub, _ = signed_in()
    found = hub.call("GET", _path())
    if args.json:
        print_json(found)
        return 0
    if not found:
        print("The hub holds no plugin: publish one with `evo-agents hub plugin publish BUNDLE`.")
        return 0
    columns = ("name", "state", "installed_version", "latest_version", "upgrade_to", "publisher")
    rows = [tuple(p[column] or "-" for column in columns) for p in found]
    table(("NAME", "STATE", "INSTALLED", "LATEST", "UPGRADE", "PUBLISHER"), rows)
    return 0


def _show(plugin: dict) -> None:
    installed = plugin["installed_version"]
    print(f"Plugin {plugin['name']} of publisher {plugin['publisher']}: {plugin['state']}")
    if installed:
        by = plugin["installed_by"] or "the hub's image"  # nobody installs a core plugin of the image
        print(f"  installed {installed} by {by} at {when(plugin['installed_at'])} UTC")
    if plugin["disabled_reason"]:
        print(f"  turned off at {when(plugin['disabled_at'])} UTC: {plugin['disabled_reason']}")
    if plugin["upgrade_to"]:
        print(f"  upgrade waiting: {plugin['upgrade_to']}")
    for version in plugin["versions"]:
        runs = "runs here" if version["compatible"] else f"does not run here: {version['incompatible_reason']}"
        print(
            f"  {version['version']}: {version['tier']}, evo-agents {version['engines']}, signed by "
            f"{version['signed_by']} ({version['signature_kind']}), {runs}"
        )
        if version["permissions"]:
            print(f"    asks for {', '.join(version['permissions'])}")


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    plugin = hub.call("GET", _path(args.name))
    if args.json:
        print_json(plugin)
    else:
        _show(plugin)
    return 0


def _changed(args, plugin: dict, verb: str) -> int:
    if args.json:
        print_json(plugin)
    elif plugin["installed_version"]:
        print(f"{verb} plugin {plugin['name']} {plugin['installed_version']}.")
    else:
        print(f"{verb} plugin {plugin['name']}; its versions stay on the hub.")
    return 0


@client_command
def cmd_install(args) -> int:
    if (args.github is None) == (args.name is None):
        raise HubError("name the plugin, or its GitHub repo with --github OWNER/REPO[@TAG], not both")
    if args.github is not None:
        return install_github(args)
    if args.signer or args.trust:
        raise HubError("--signer and --trust go with --github")
    hub, _ = signed_in()
    return _changed(args, hub.call("POST", _path(args.name, "install"), {"version": args.version}), "Installed")


@client_command
def cmd_upgrade(args) -> int:
    hub, _ = signed_in()
    return _changed(args, hub.call("POST", _path(args.name, "upgrade"), {"version": args.version}), "Upgraded")


@client_command
def cmd_remove(args) -> int:
    hub, _ = signed_in()
    return _changed(args, hub.call("POST", _path(args.name, "remove")), "Removed")


def _name(parser) -> None:
    parser.add_argument("name", metavar="NAME", help="the plugin's id, as its manifest gives it")


def register_plugins(hsub) -> None:
    plugin = hsub.add_parser(
        "plugin", help="the hub's plugins: publish and install them (hub admins), enable and configure them per project"
    )
    psub = plugin.add_subparsers(dest="plugin_command", required=True)

    published = psub.add_parser("publish", help="upload a signed plugin bundle and publish it as its version")
    published.add_argument("bundle", metavar="BUNDLE", help="the plugin's tar.gz, evo-plugin.json at its top")
    published.add_argument(
        "--signature", metavar="FILE", help="its signature (default: BUNDLE.sig.json or BUNDLE.sigstore.json)"
    )
    json_option(published, returns_object(*PUBLISHED_KEYS, schema="PluginPublished"))
    published.set_defaults(func=cmd_publish)

    listed = psub.add_parser("list", help="every plugin of the hub: state, installed and latest version")
    listed.add_argument("--project", metavar="PROJECT", help="the installed plugins as the project has them")
    store = returns_array(*PLUGIN_KEYS, schema="Plugin")
    json_option(listed, JsonOutput("array", store.keys, store.schema, variants=(("--project", project_list_output()),)))
    listed.set_defaults(func=cmd_list)

    show = psub.add_parser("show", help="one plugin with every version, its signer and what it asks for")
    _name(show)
    json_option(show, returns_object(*DETAIL_KEYS, schema="PluginDetail"))
    show.set_defaults(func=cmd_show)

    install = psub.add_parser(
        "install", help="install a version: the newest that runs on the hub, or --version; or a GitHub release"
    )
    install.add_argument("name", metavar="NAME", nargs="?", help="the plugin's id, as its manifest gives it")
    install.add_argument("--version", metavar="VERSION", help="the version, such as 1.2.0")
    install.add_argument(
        "--github", metavar="OWNER/REPO[@TAG]", help="install from the repo's GitHub release: the latest, or TAG"
    )
    install.add_argument(
        "--signer",
        help="with --github: what signs its releases (default: sigstore:OWNER/REPO/.github/workflows/ci.yml)",
    )
    install.add_argument("--trust", action="store_true", help="with --github: trust the signer if the hub does not yet")
    github = returns_object(*INSTALLED_KEYS, schema="MarketInstalled")
    json_option(install, returns_object(*PLUGIN_KEYS, schema="Plugin", variants=(("--github", github),)))
    install.set_defaults(func=cmd_install)

    upgrade = psub.add_parser(
        "upgrade", help="move the installed version to a newer one: the newest that runs, or --version"
    )
    _name(upgrade)
    upgrade.add_argument("--version", metavar="VERSION", help="the version, such as 1.2.0")
    json_option(upgrade, returns_object(*PLUGIN_KEYS, schema="Plugin"))
    upgrade.set_defaults(func=cmd_upgrade)

    removed = psub.add_parser("remove", help="remove the installed version; every version stays on the hub")
    _name(removed)
    json_option(removed, returns_object(*PLUGIN_KEYS, schema="Plugin"))
    removed.set_defaults(func=cmd_remove)
    register_project_commands(psub, _name)
    register_publishers(psub)
