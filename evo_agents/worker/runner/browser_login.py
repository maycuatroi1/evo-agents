"""A login session (kind ``browser``, ``BrowserLoginRun``, ``evo_agents.hub.browser_login``): Chromium on a browser
identity's profile, which a person logs in through the live view, in human mode from the start.

The run has no agent and no repo. The daemon starts Chromium as it does for any run with browser identities
(``Agent.open_browser``: the identity's current profile, an empty one for an identity never saved and for a Move, where
the profile does not leave its worker), opens the identity's login URL (the lease says it) and reports ``interactive``,
the state in which the live view is human. It then waits for the owner to press Save (the finish of the run), for the
owner to cancel it, or for the timeout of 30 minutes from the moment Chromium runs.

Save closes Chromium, packs the profile and sends it to the hub (``Agent.save_browser``), which seals it and raises the
identity's version; the run then ends done. A save that did not go through fails the run with cause ``environment``.
Cancel and the timeout end the run without a save (``saves_browser``), and in every case ``runs/<run>/browser``, the
profile directory, goes with the run (``BrowserSession.close``).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.worker.browser import profiles
from evo_agents.worker.runner.common import RunFailed, log

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

POLL = 1.0  # seconds between looks at the run's timeout and its stop


class BrowserLoginRun:
    """A login session: its Chromium, its live view in human mode, and a Save that seals the profile."""

    parks = False
    chat = True  # the owner's Save is the finish of the run (Run.request_finish)

    def __init__(self, run: Run):
        self.run = run
        run.title = run.spec.get("title") or "Browser login"

    @property
    def saves_browser(self) -> bool:
        """Whether the profile goes to the hub as the run ends: only when the owner pressed Save."""
        return self.run.finish_asked.is_set()

    async def steps(self) -> None:
        run = self.run
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: a browser login session of project "
            f"{run.project}, at most {run.timeout_s // 60} min. Chromium runs for a person to sign in through the "
            "live view; Save keeps the profile, cancelling keeps nothing."
        )
        run.check()
        await run.agent.open_browser()
        run.agent_starts()
        browser = run.agent.browser
        await self.open_page(browser.profiles.login_url if browser is not None else None)
        await run.reports.report("interactive")
        run.note("Browser: waiting for the person to sign in and press Save.")
        while not run.finish_asked.is_set():
            run.check()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wait(), POLL)
        run.check()
        await run.agent.save_browser()
        if browser is None or not browser.profiles.sent:
            raise RunFailed("the profile was not saved: the hub keeps the identity at its version", cause="environment")
        await run.reports.report("verifying")
        await run.reports.end("done", summary="The browser profile was saved.")

    async def wait(self) -> None:
        await asyncio.wait(
            [asyncio.ensure_future(self.run.finish_asked.wait()), asyncio.ensure_future(self.run.stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def open_page(self, url: str | None) -> None:
        """Open ``url`` in Chromium's first page; a page that does not open is noted, the person types the address."""
        browser = self.run.agent.browser
        if not url or browser is None or not browser.cdp:
            return
        try:
            async with profiles.devtools(browser.cdp, browser.socket or "") as tools:
                pages = [
                    t for t in (await tools("Target.getTargets")).get("targetInfos") or [] if t.get("type") == "page"
                ]
                if not pages:
                    await tools("Target.createTarget", {"url": url})
                    return
                attached = await tools("Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True})
                await tools("Page.navigate", {"url": url}, session=attached["sessionId"])
        except (profiles.DevToolsFailed, KeyError) as exc:
            log.warning("login page not opened", extra={"run_id": self.run.id, "error": str(exc)})
            self.run.note("Browser: the login page did not open; type its address in the URL box of the view.")

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return []

    async def diff(self) -> bytes:
        return b""

    async def release_branch(self) -> None:
        return None
