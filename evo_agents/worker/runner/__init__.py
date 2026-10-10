"""The parts of a run on this worker (``evo_agents.worker.run`` keeps the names the daemon and the tests import).

- ``common``: the exceptions that end a run or one of its steps, and the small helpers every part uses.
- ``sender``: the spool's events on their way to the hub.
- ``transitions``: the decisions of a run that need no I/O (how it ends, what it reports, which branch it works on,
  when the watchdog stops it), each a pure function a test calls without a daemon or a hub.

This package imports nothing on its own, so a part that needs only the core package (``transitions``) loads
without the worker extra.
"""
