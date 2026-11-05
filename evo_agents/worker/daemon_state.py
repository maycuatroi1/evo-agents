"""What a heartbeat answer says of the worker itself (``daemon``, ``daemon_heartbeat``).

- Whether to drain: no new claims while it does.
- The worker's settings as the hub holds them (slots, name, labels, plugins), which its owner may change from the web.
  When they differ from config.json, the daemon takes them in memory, writes config.json again (the other fields as
  they are on disk) and logs the old and new values: more slots are claimed at the next turn of the claim loop, and
  fewer stop no run, the daemon only claims nothing until it holds fewer runs than the new slots. An answer without
  settings (a hub before schema 0026) changes nothing, and one without plugins (a hub before 0039) leaves them.
- The declarations of the runtimes of the plugins its owner accepted (``plugin_runtimes``): each one, of a plugin of
  config.json's plugins, gets its adapter of ACP (``acp.adapter_class``), and the next heartbeat looks for their
  programs and reports them beside the core's runtimes.
- For the self-update, the hub's read of nightly and its owner's Update now; the owner's Install browser
  (``daemon_browser``) and the graphs to keep fresh (``daemon_kg``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.plugins.runtimes import AcpRuntime
from evo_agents.worker import guard
from evo_agents.worker.runtimes.acp import adapter_class

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def settings_of(value) -> dict | None:
    """The worker settings of a heartbeat answer, {"slots", "name", "labels"}, "plugins" when the hub sends them
    (0039), "version_locked" (0048) and "projects" (evo-ak 0.31.2), or None when they are not as the hub sends them."""
    if not isinstance(value, dict):
        return None
    slots, name, labels = value.get("slots"), value.get("name"), value.get("labels")
    if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= runs.MAX_SLOTS:
        return None
    if not isinstance(name, str) or not name or not _strings(labels):
        return None
    found = {"slots": slots, "name": name, "labels": list(labels or ())}
    if "plugins" in value:
        if not _strings(value["plugins"]) or len(value["plugins"]) > runs.MAX_WORKER_PLUGINS:
            return None
        found["plugins"] = list(value["plugins"])
    if "version_locked" in value:
        if not isinstance(value["version_locked"], bool):
            return None
        found["version_locked"] = value["version_locked"]
    if "projects" in value:  # a worker serves one project at least: none is a hub that sent no list
        if not _strings(value["projects"]):
            return None
        if value["projects"]:
            found["projects"] = list(value["projects"])
    return found


class WorkerState:
    """Takes what heartbeat answers say of ``daemon``'s worker into the daemon and its config.json."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.settings_unread = False
        self.declarations_unread: set[str] = set()  # the errors of declarations logged already
        self.guarding: asyncio.Task | None = None  # the fetch of the hub's guard policy, while it runs

    def take(self, answer: dict) -> None:
        """What a heartbeat answer says of the worker itself: whether to drain, its settings when the hub sends them,
        and for the self-update, the hub's read of nightly and its owner's Update now."""
        daemon = self.daemon
        drain = bool(answer.get("drain"))
        if drain != daemon.draining:
            log.info("draining: no new claims" if drain else "drain ended: claiming again")
            daemon.draining = drain
            daemon.slot_free.set()
        if answer.get("settings") is not None:
            self._take_settings(answer["settings"])
            self.guarding = guard.heard(self.guarding, daemon.hub, daemon.home.root, answer["settings"])
        else:
            daemon.updater.hear_lock(None)
        daemon.updater.hear_answer(answer.get("nightly"), answer.get("update_now"))
        daemon.browser.take(answer.get("browser_install"))
        self._take_plugin_runtimes(answer.get("plugin_runtimes"))
        if daemon.graphs.on:
            daemon.graphs.take_duties(answer.get("kg_sync"))

    def _take_plugin_runtimes(self, value) -> None:
        """The declarations of the runtimes of the plugins the owner accepted, as the answer carries them: each one of
        a plugin of the config's plugins gets its adapter of ACP, looked for at the next heartbeat when they changed. A
        declaration this daemon cannot read is logged once and left out; an answer without any (a hub before 0039)
        leaves none."""
        daemon = self.daemon
        found: dict[str, AcpRuntime] = {}
        for item in value if isinstance(value, list) else []:
            try:
                declaration = AcpRuntime.from_json(item)
            except ValueError as exc:
                if str(exc) not in self.declarations_unread:
                    self.declarations_unread.add(str(exc))
                    log.warning("the hub declared a runtime this daemon cannot read", extra={"error": str(exc)})
                continue
            if declaration.plugin in daemon.config.plugins:
                found[declaration.runtime] = declaration
        if found == daemon.plugin_runtimes:
            return
        daemon.plugin_runtimes = found
        daemon.plugin_adapters = {name: adapter_class(declaration) for name, declaration in found.items()}
        daemon.look_again(runtimes=True)  # looked for at the next heartbeat
        log.info("runtimes of plugins declared", extra={"runtimes": sorted(found)})

    def _take_settings(self, settings) -> None:
        """Take the worker's settings the hub sent (slots, name, labels, plugins, version_locked, projects) when they
        differ from the config: in memory, and in config.json, read again so a field set by hand since the start
        (checkouts) stays. New projects have the checkouts looked at again at the next turn. Settings this daemon
        cannot read are logged and left, and leave the lock unknown."""
        daemon = self.daemon
        wanted = settings_of(settings)
        daemon.updater.hear_lock(None if wanted is None else wanted.get("version_locked"))
        if wanted is None:
            if not self.settings_unread:  # once, not at each heartbeat
                self.settings_unread = True
                log.warning("the hub sent worker settings this daemon cannot read", extra={"got": settings})
            return
        self.settings_unread = False
        old = {key: getattr(daemon.config, key) for key in wanted}
        if old == wanted:
            return
        self._save(wanted, projects_changed="projects" in wanted and old["projects"] != wanted["projects"])
        changed = {key: {"old": old[key], "new": wanted[key]} for key in wanted if old[key] != wanted[key]}
        log.info("worker settings changed on the hub", extra={"changed": changed})
        if "slots" in changed:
            daemon.slot_free.set()  # more slots: the claim loop claims at once
        if "projects" in changed:
            daemon.look_again(checkouts=True)  # the checkouts of the projects it serves now

    def _save(self, wanted: dict, *, projects_changed: bool) -> None:
        """The settings ``wanted`` in memory and in config.json."""
        daemon = self.daemon
        for key, value in wanted.items():
            setattr(daemon.config, key, value)
        try:
            on_disk = daemon.home.load_config()
        except Exception:  # an unreadable config.json: write the one in memory, which holds the new settings
            on_disk = daemon.config
        for key, value in wanted.items():
            setattr(on_disk, key, value)
        if projects_changed:
            daemon.config.repos = on_disk.repos  # those `worker projects` on this machine read for new projects
        try:
            daemon.home.save(on_disk)
        except OSError as exc:
            log.error("config.json could not be written; the new settings hold until the daemon stops", exc_info=exc)
