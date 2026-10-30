"""Whether a step about to run touches files another run of the same plan and repo is editing now. Pure functions over
JSON values, standard library only; the hub's claim (``evo_agents.hub.server.runs.service.overlap``) feeds them.

A step names the paths it works on in its ``what`` and ``note`` (``step_paths``): a word with a known file extension, or
a directory ending in a slash. The files a running run edited come from its ``tool_call`` events, read as
``evo_agents.hub.facets`` reads them (``edited_paths``). A path of a step meets an edited file when the file ends with
it at a directory boundary (the events hold absolute paths of the worker's worktree) or, for a directory, lies in it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from evo_agents.hub import facet_readers

EXTENSIONS = (
    "py|pyi|ts|tsx|js|jsx|mjs|css|html|md|rst|txt|yaml|yml|json|toml|ini|cfg|sql|sh|rs|go|java|kt|rb|lock|ipynb"
)
FILE = re.compile(rf"(?<![\w./-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:{EXTENSIONS}))(?![\w])")
FOLDER = re.compile(r"(?<![\w./-])((?:[\w.-]+/)+)(?![\w.-])")
MAX_PATHS = 50
MAX_SHOWN = 10


def step_paths(*texts: object) -> list[str]:
    """The paths the texts of a step (its what and note) name, in order, each once: files with a known extension, and
    directories written with a trailing slash. A URL's path is no path of the repo."""
    found: dict[str, None] = {}
    for text in texts:
        if not isinstance(text, str):
            continue
        text = re.sub(r"\w+://\S+", " ", text)
        for pattern in (FILE, FOLDER):
            for match in pattern.finditer(text):
                path = match.group(1).lstrip("./")
                if path and len(found) < MAX_PATHS:
                    found.setdefault(path)
    return list(found)


def edited_paths(runtime: str | None, events: Iterable) -> list[str]:
    """The files the edit tools wrote in a run, from its events in seq order."""
    return list(facet_readers.run_facet(runtime, events).get("files_edited") or [])


def meets(path: str, edited: str) -> bool:
    """Whether the step's ``path`` names the file ``edited`` (or a directory it lies in)."""
    edited = edited.replace("\\", "/")
    if path.endswith("/"):
        return f"/{path}" in f"/{edited}" or edited.startswith(path)
    return edited == path or edited.endswith(f"/{path}")


def overlapping(paths: Iterable[str], edited: Iterable[str]) -> list[str]:
    """The files of ``edited`` that a path of ``paths`` names, in order, at most MAX_SHOWN."""
    paths = list(paths)
    hit = [file for file in edited if any(meets(path, file) for path in paths)]
    return hit[:MAX_SHOWN]
