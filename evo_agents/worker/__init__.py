"""The worker daemon: ``evo-agents worker``. It runs plan steps the hub hands this machine, with a coding agent of the
machine's owner, and sends their logs, state and evidence back (``docs/workers.md``, protocol version 1).

The group is not ``evo-agents hub worker``, which is the hub server's own job worker.

Modules, each with one concern:

- ``home``: the state under ``~/.evo/worker`` (directory 0700, token and config 0600, spool, runs, worktrees).
- ``logs``: JSON log lines in ``~/.evo/worker/worker.log``, tokens removed.
- ``adapter``: the interface between the daemon and an agent runtime, the loading of adapters, and which runtimes
  this machine has (from PATH and ``--version``).
- ``checkouts``: the checkouts of the worker's projects, from the harness registry and the hub's project repos.
- ``hubapi``: the worker's HTTP calls to the hub (aiohttp), with the protocol header and the backoff.
- ``spool``: a run's events on disk until the hub acknowledges them.
- ``gitops``: the git commands of a run: fetch, worktree, commit, push.
- ``run``: one run from claim to its last report.
- ``daemon``: the claim loop, the heartbeat, signals and the cleanup of old worktrees.
- ``power``: the power assertions that keep the machine awake while the daemon holds runs, and noticing a sleep.
- ``runtimes``: the adapters of Claude Code, opencode and Codex, through each runtime's SDK or API.
- ``interactive``: takeover and handback, the runtimes' terminal UIs in tmux, their logs, and the worker's end of the
  web terminal.
- ``selftest``: ``evo-agents worker selftest``, one real run of an adapter in a scratch repository.
- ``service``: the daemon in the background, as a launchd LaunchAgent or a systemd user unit.
- ``cli``: the commands.

Only ``hubapi``, ``daemon`` and ``run`` need the worker extra (``evo-ak[worker]``) to load, and the adapters of
``runtimes`` need it once an agent starts; the others need the core package, so ``evo-agents`` starts on a core
install and the commands name the extra when it is missing.
"""
