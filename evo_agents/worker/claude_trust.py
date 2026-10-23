"""Claude Code's folder trust: ``trust_folder`` marks folders trusted in its state file, so an interactive run
(``evo_agents.worker.interactive``) never stops at the trust dialog that comes in every new folder.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from evo_agents.hub.client import write_atomic


class TrustError(Exception):
    """The state file cannot be read as a JSON object, so it is left as it is."""


def trust_folder(path: Path, folders: Iterable[str]) -> bool:
    """Set ``projects[<folder>].hasTrustDialogAccepted`` to true in Claude Code's state file ``path`` for each of
    ``folders``, keeping every other key; whether the file changed. The file is replaced atomically, with its mode,
    and through a symbolic link to where it points. A file that is not a JSON object raises TrustError untouched."""
    target = Path(os.path.realpath(path))
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        raise TrustError(f"cannot read {path}: {exc}") from None
    try:
        state = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise TrustError(f"{path} is not JSON ({exc}); it is left as it is") from None
    if not isinstance(state, dict):
        raise TrustError(f"{path} is not a JSON object; it is left as it is")
    projects = state.get("projects")
    if projects is None:
        projects = state["projects"] = {}
    if not isinstance(projects, dict):
        raise TrustError(f"projects in {path} is not a JSON object; it is left as it is")
    changed = False
    for folder in folders:
        entry = projects.get(folder)
        if entry is None:
            entry = projects[folder] = {}
        if not isinstance(entry, dict):
            raise TrustError(f"projects[{folder!r}] in {path} is not a JSON object; it is left as it is")
        if entry.get("hasTrustDialogAccepted") is not True:
            entry["hasTrustDialogAccepted"] = True
            changed = True
    if changed:
        try:
            mode = target.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(state, indent=2, ensure_ascii=False).encode() + b"\n"
        write_atomic(target, data, mode)
    return changed
