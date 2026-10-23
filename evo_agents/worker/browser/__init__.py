"""The browser of a run dispatched with browser (plan agent-browser): ``install`` puts Chromium on the worker,
``chromium`` says how the daemon starts it, ``session`` starts and stops it for a run, ``cli`` is
``evo-agents worker browser install``."""

from evo_agents.worker.browser.cli import register_browser

__all__ = ["register_browser"]
