"""The Claude account a local session runs as: its email and, when it can be told, the claude-profiles profile.

A session of the Code tab of Claude Desktop has a record, ``<data folder>/claude-code-sessions/<account>/<org>/
local_<id>.json``, in the data folder of the profile that runs it (claude-profiles starts Desktop with
``--user-data-dir``). The folders to look in are those of claude-profiles (``profiles.json`` in its application
support folder, read by what it holds under any of a few key names, since its format is not ours) and Desktop's own
(``~/Library/Application Support/Claude``). A record is the one whose text holds the session id; the email is the first
value under a key with ``email`` in its name, and the profile is the name claude-profiles gave the folder. A session of
the CLI has none: its account is ``oauthAccount.emailAddress`` of the configuration Claude Code uses
(``$CLAUDE_CONFIG_DIR/.claude.json``, else ``~/.claude.json``).

Nothing here fails: what cannot be read is ``unknown`` (the email) or None (the profile), and no secret is read. Only
the keys with ``email`` in their name, the profile names and the folder paths are taken from the files. Standard
library only. The layout of a Desktop record is [Unverified] on the machines this ran on: it follows the README of
claude-profiles, and ``EVO_CLAUDE_DESKTOP_DIRS`` (``name=path`` or ``path``, separated like PATH) names the folders
when the defaults miss.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "unknown"
FOLDERS_VARIABLE = "EVO_CLAUDE_DESKTOP_DIRS"
SESSIONS = "claude-code-sessions"
MAX_RECORDS = 300  # newest records read per data folder
MAX_RECORD_BYTES = 1 << 20
MAX_DEPTH = 4  # levels of a record searched for an email
NAME_KEYS = ("name", "displayName", "title", "label")
FOLDER_KEYS = ("dataFolder", "dataDir", "dataDirectory", "userDataDir", "path", "folder", "dir")
PROFILES_FILE = Path("Library/Application Support/tech.omelet.ClaudeProfiles/profiles.json")
DEFAULT_FOLDER = Path("Library/Application Support/Claude")


@dataclass(frozen=True)
class Account:
    email: str = UNKNOWN
    profile: str | None = None


def _json(path: Path, limit: int = MAX_RECORD_BYTES):
    try:
        with path.open("rb") as handle:
            return json.loads(handle.read(limit))
    except (OSError, ValueError):
        return None


def _first(item: Mapping, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _profile_entries(data, depth: int = 0):
    """(name, folder) of every object in the profile list that gives a folder, found at any depth up to 3."""
    if isinstance(data, list):
        for item in data:
            yield from _profile_entries(item, depth + 1)
    elif isinstance(data, dict):
        folder = _first(data, FOLDER_KEYS)
        if folder is not None:
            yield _first(data, NAME_KEYS), folder
        elif depth < 3:
            for item in data.values():
                if isinstance(item, list | dict):
                    yield from _profile_entries(item, depth + 1)


def data_folders(env: Mapping[str, str], home: Path) -> list[tuple[str | None, Path]]:
    """(profile name or None, data folder) to look for session records in, the named ones first."""
    given = env.get(FOLDERS_VARIABLE)
    if given:
        folders = []
        for part in given.split(os.pathsep):
            name, sep, path = part.partition("=")
            folders.append((name or None, Path(path)) if sep else (None, Path(part)))
        return [(name, path) for name, path in folders if str(path)]
    found: list[tuple[str | None, Path]] = []
    for name, folder in _profile_entries(_json(home / PROFILES_FILE)):
        path = Path(folder).expanduser()
        if path.is_absolute():
            found.append((name, path))
    found.append((None, home / DEFAULT_FOLDER))
    return found


def _email_in(value, depth: int = 0) -> str | None:
    if depth > MAX_DEPTH:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if "email" in str(key).lower() and isinstance(item, str) and "@" in item and len(item) <= 320:
                return item.strip()
        for item in value.values():
            found = _email_in(item, depth + 1) if isinstance(item, dict | list) else None
            if found:
                return found
    elif isinstance(value, list):
        for item in value[:20]:
            found = _email_in(item, depth + 1)
            if found:
                return found
    return None


def _records(folder: Path) -> list[Path]:
    root = folder / SESSIONS
    found = []
    with contextlib.suppress(OSError):
        for path in root.glob("*/*/local_*.json"):
            with contextlib.suppress(OSError):
                found.append((path.stat().st_mtime, path))
    return [path for _, path in sorted(found, reverse=True)[:MAX_RECORDS]]


def desktop_account(session_id: str, folders: list[tuple[str | None, Path]]) -> Account | None:
    """The account the Desktop record of ``session_id`` gives, None when no record names the session."""
    for name, folder in folders:
        for path in _records(folder):
            try:
                text = path.read_text(encoding="utf-8")[:MAX_RECORD_BYTES]
            except (OSError, ValueError):
                continue
            if session_id not in text:
                continue
            try:
                email = _email_in(json.loads(text))
            except ValueError:
                email = None
            return Account(email or UNKNOWN, name)
    return None


def cli_email(env: Mapping[str, str], home: Path) -> str | None:
    """``oauthAccount.emailAddress`` of the configuration this Claude Code reads."""
    base = env.get("CLAUDE_CONFIG_DIR")
    for path in ((Path(base) / ".claude.json",) if base else ()) + (home / ".claude.json",):
        data = _json(path, 4 << 20)
        account = data.get("oauthAccount") if isinstance(data, dict) else None
        email = account.get("emailAddress") if isinstance(account, dict) else None
        if isinstance(email, str) and "@" in email:
            return email.strip()
        if base:
            break  # a profile's own configuration is the only one that says whose session this is
    return None


def read_account(session_id: str, env: Mapping[str, str] | None = None, home: Path | None = None) -> Account:
    """The account of the session, ``Account('unknown')`` when nothing says; never raises."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    try:
        found = desktop_account(session_id, data_folders(env, home))
        if found is not None:  # the CLI's account is not a Desktop profile's: no fallback to it
            return found
        return Account(cli_email(env, home) or UNKNOWN)
    except Exception:  # a hook never fails the session
        return Account()
