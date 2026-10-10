"""The parts of a run on this worker (``evo_agents.worker.run`` keeps the names the daemon and the tests import).

- ``context``: the ``Run`` the daemon holds, which composes the parts below and the run's kind, and ``RunKind``, the
  Protocol each kind meets.
- ``kinds``: the kind of a run by its claim: ``step``, ``plan``, ``review``, ``judge`` and ``author``, one module
  each. No kind inherits from another or from the Run: what two kinds share is a part both compose.
- Parts every kind shares: ``sender`` (the spool's events on their way to the hub), ``reports`` (the moves reported,
  the log and the diff uploaded), ``agent`` and ``terminal`` (the agent, headless or in its terminal UI), ``inbox``
  (the owner's messages), ``watchdog`` (the budget, and the watchdog of a run of the Curator), and the functions of
  ``access`` (the leases and the preflight), ``origin`` (fetching, and the branch a worktree works on), ``verify``
  (the result file and the verify commands) and ``push`` (the commit and the owner's notices).
- Parts the kinds over several repos share (plan, review, judge, author): ``directory`` (the run's directory and its
  worktrees, the review's read-only ones among them) and ``conversation`` (the turns of the agent in one session, and
  the waits for the owner between them).
- ``common``: the exceptions that end a run or one of its steps, and the small helpers every part uses.
- ``transitions``: the decisions of a run that need no I/O (how it ends, what it reports, which branch it works on,
  when the watchdog stops it), each a pure function a test calls without a daemon or a hub.

This package imports nothing on its own, so a part that needs only the core package (``transitions``) loads
without the worker extra.
"""
