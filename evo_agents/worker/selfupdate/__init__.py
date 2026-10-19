"""The worker daemon updates its own install to the tag ``nightly`` of maycuatroi1/evo-agents, while it is idle.

- ``install``: what the daemon runs from. Only a ``uv tool install`` of evo-ak from this repository by git, or from
  PyPI, can be updated; an editable install, a local path, another repository or an install without a receipt cannot,
  and the daemon says why.
- ``supervisor``: whether launchd or systemd started the daemon and starts it again after it exits with
  ``home.EXIT_UPDATED``. A daemon run by hand never updates itself.
- ``peers``: the daemons that run from one install (two workers of one machine may share it) know each other's state
  through a directory of their own under ``~/.evo/installs``, and two file locks keep a claim and an update apart.
- ``installer``: where the tag points (``git ls-remote``, else the GitHub API), and the update itself: ``uv tool
  install`` into a staging directory first, then over the install, each checked to start and to carry the commit, and
  a copy of the old install put back when the new one does not start.
- ``backup``: that copy of the install and its commands, made before uv goes over the install, since some uv releases
  delete the environment first.
- ``updater``: ``SelfUpdate``, the decisions of each turn of the daemon and what its heartbeats say of them.

Standard library only.
"""
