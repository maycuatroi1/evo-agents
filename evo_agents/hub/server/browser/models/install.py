"""The limits of an Install browser (``service.install``): when the hub asks a worker again, when an open request no
longer counts as a wait, how many queued runs a pass reads, and what a daemon too old for it lacks."""

from __future__ import annotations

from datetime import timedelta

from evo_agents.hub import runs

NOT_SAID = f"its daemon does not say {runs.BROWSER_INSTALL} in its features"
REASK_AFTER = timedelta(hours=24)  # a request that ended without Chromium: the hub does not open another before
UNFINISHED_AFTER = timedelta(minutes=30)  # an open request older than this no longer counts as a wait (queue_fit)
ASK_BATCH = 50  # the queued runs with browser one pass of ``ask_for_queued`` reads
WITHOUT_CHROMIUM = ("cancelled", "failed", "impossible")
