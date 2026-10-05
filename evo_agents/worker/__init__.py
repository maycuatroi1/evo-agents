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
- ``cli``: the commands.

Only ``hubapi``, ``daemon`` and ``run`` need the worker extra (``evo-ak[worker]``); the others need the core package,
so ``evo-agents`` starts on a core install and the commands name the extra when it is missing.
"""
