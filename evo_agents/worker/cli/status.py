"""``evo-agents worker status [--json]``: what this machine is to the hub, and what the daemon sees here: its
runtimes, tmux, the checkouts of its projects, the runs kept here and the sync of the knowledge graph."""

from __future__ import annotations

import os

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub.api import paths
from evo_agents.hub.client import HubError
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext
from evo_agents.worker.home import WorkerConfig, WorkerHome, dir_bytes
from evo_agents.worker.membership import member_hub


def hub_view_of(config: WorkerConfig) -> tuple[dict | None, str | None]:
    """The worker as the hub shows it to the member signed in here, or why it could not be read."""
    member = member_hub(config.url)
    if member is None:
        return None, None
    try:
        return member.call("GET", paths.worker(config.worker_id)), None
    except HubError as exc:
        return None, str(exc)


def status_lines(home: WorkerHome, config: WorkerConfig, status: dict, tmux) -> list[str]:
    """The status as text, a line for each thing it holds."""
    pid, hub_view, hub_error, found = status["daemon_pid"], status["hub"], status["hub_error"], status["checkouts"]
    lines = [
        f"Worker {config.name} (id {config.worker_id}) of {config.url}, owner {config.owner or 'unknown'}",
        f"  projects: {', '.join(config.projects) or 'none'}; slots: {config.slots}; labels: "
        f"{', '.join(config.labels) or 'none'}",
        f"  daemon: {'running, pid ' + str(pid) if pid else 'not running'}",
    ]
    if hub_view is not None:
        lines.append(
            f"  on the hub: {hub_view.get('status')}, last heartbeat {hub_view.get('last_heartbeat_at') or 'never'}, "
            f"{hub_view.get('held_runs', 0)} run(s) held"
        )
    elif hub_error:
        lines.append(f"  on the hub: unknown ({hub_error})")
    else:
        lines.append(f"  on the hub: unknown (sign in with `evo-agents hub login --url {config.url}` to see it)")
    for name, report in status["runtimes"].items():
        state = "available" if report["available"] else f"unavailable ({report.get('reason') or 'no reason given'})"
        lines.append(f"  runtime {name}: {report.get('version') or '-'}, {state}")
    if tmux.available:
        terminal = "allowed" if config.allow_web_terminal else "not allowed"
        lines.append(
            f"  interactive runs: tmux {status['tmux']['version'] or '?'} at {tmux.binary}; web terminal {terminal}"
        )
    else:
        lines.append("  interactive runs: unsupported (tmux is not on PATH); headless runs only")
    lines += [
        f"  checkout {key}: {found[key]['path']} ({found[key].get('branch') or 'detached'})" for key in sorted(found)
    ]
    if not found:
        lines.append("  no checkout of the worker's projects found: each run clones the repos it needs")
    records = status["runs"]
    unfinished = [r for r in records if not r.get("finished_at")]
    lines.append(
        f"  runs kept here: {len(records)} ({len(unfinished)} not ended); spool: {status['spool_bytes']} bytes"
    )
    return lines + [f"  {line}" for line in [*home.kg_sync_lines(), f"state: {home.root}"]]


class Status(WorkerCommand):
    name = "status"
    help = "this worker as the hub and this machine see it"
    arguments = (Arg("--json", action="store_true", help="machine-readable output"),)

    def run(self, ctx: WorkerContext) -> Result:
        from evo_agents.worker.adapter import detect_runtimes, load_adapters
        from evo_agents.worker.checkouts import discover
        from evo_agents.worker.interactive import Tmux

        home = ctx.home
        config = home.load_config()
        runtimes = detect_runtimes(load_adapters())
        tmux = Tmux.from_env(os.environ)
        found = discover(config)
        records = home.load_runs()
        pid = home.read_pid()
        hub_view, hub_error = hub_view_of(config)
        status = {
            "url": config.url,
            "worker_id": config.worker_id,
            "name": config.name,
            "owner": config.owner,
            "projects": config.projects,
            "slots": config.slots,
            "labels": config.labels,
            "daemon_pid": pid,
            "runtimes": runtimes,
            "tmux": {"path": tmux.binary, "version": tmux.version()},
            "allow_web_terminal": config.allow_web_terminal,
            "checkouts": found,
            "spool_bytes": dir_bytes(home.spool_dir),
            "runs": [{k: r.get(k) for k in ("id", "state", "worktree", "finished_at")} for r in records],
            "hub": hub_view,
            "hub_error": hub_error,
            "kg_sync": home.kg_sync(),
            "state_dir": str(home.root),
        }
        return Result(status, Text(*status_lines(home, config, status, tmux)))


STATUS = Status()
