"""The Claude account a local session runs as: its email and, when it can be told, the claude-profiles profile.

Claude Desktop runs a session of its Code tab with ``CLAUDE_CODE_USER_EMAIL`` (the account) and
``CLAUDE_CODE_ACCOUNT_UUID`` in the environment, which the hooks inherit; ``CLAUDE_CODE_EXECPATH``, when set, is a
claude binary under ``<data folder>/claude-code/``. claude-profiles starts each profile's Desktop with
``--user-data-dir``, and each data folder keeps the records of its one account,
``<data folder>/claude-code-sessions/<account>/<org>/local_<id>.json``, so the only folder with records of that account
is the profile's; failing that, the one that holds the binary, if it has no records of another account. On
binhna-macbook-m4 (2026-10-19) a Desktop started from inside a Claude Code session kept that session's
``CLAUDE_CODE_EXECPATH``, and so did the CLI it started and the hooks under it (the session's tools see the CLI's own):
a binary of the default profile, which filed a session of another profile under it. The folders are those of
claude-profiles (``profiles.json`` in its application support folder, read by what it holds under any of a few key
names, since its format is not ours; a profile with a name and no folder is Desktop's own) and Desktop's own
(``~/Library/Application Support/Claude``), and the profile is the name claude-profiles gave the folder.

An older Desktop that sets none of these leaves the record to go by: the one whose text holds the session id, its
email the first value under a key with ``email`` in its name. On binhna-macbook-m4 (Desktop 2.42304, 2026-10-18) a
record has no email, may not hold the CLI session id yet at the session's first prompt, and claude-profiles copies it
into every profile that shares sessions, so a record names the profile only when sharing is off. A session of the CLI
has none of this: its account is ``oauthAccount.emailAddress`` of the configuration Claude Code uses
(``$CLAUDE_CONFIG_DIR/.claude.json``, else ``~/.claude.json``).

Nothing here fails: what cannot be read is ``unknown`` (the email) or None (the profile), and no secret is read. Only
the keys with ``email`` in their name, the profile names and the folder paths are taken from the files. Standard
library only. ``EVO_CLAUDE_DESKTOP_DIRS`` (``name=path`` or ``path``, separated like PATH) names the folders when the
defaults miss.
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
EMAIL_VARIABLE = "CLAUDE_CODE_USER_EMAIL"  # set by Claude Desktop for a session of its Code tab
ACCOUNT_VARIABLE = "CLAUDE_CODE_ACCOUNT_UUID"
BINARY_VARIABLE = "CLAUDE_CODE_EXECPATH"
BINARIES = "claude-code"  # where Desktop keeps the claude binaries it runs, in its data folder
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
    """(name, folder) of every object in the profile list that gives a folder, found at any depth up to 3; folder
    None for a named profile in a list that gives none, Desktop's own (claude-profiles' default has no folder)."""
    if isinstance(data, list):
        for item in data:
            if (
                isinstance(item, dict)
                and _first(item, NAME_KEYS)
                and _first(item, FOLDER_KEYS) is None
                and not any(isinstance(value, list | dict) for value in item.values())
            ):
                yield _first(item, NAME_KEYS), None
            else:
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
    own: str | None = None
    for name, folder in _profile_entries(_json(home / PROFILES_FILE)):
        if folder is None:
            own = own or name
            continue
        path = Path(folder).expanduser()
        if path.is_absolute():
            found.append((name, path))
    found.append((own, home / DEFAULT_FOLDER))
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


def _inside(path: str, folder: Path) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(folder) + os.sep)


def _accounts(folder: Path) -> set[str]:
    """The accounts whose session records the data folder keeps."""
    with contextlib.suppress(OSError):
        return {path.name for path in (folder / SESSIONS).iterdir() if path.is_dir()}
    return set()


def running_profile(env: Mapping[str, str], folders: list[tuple[str | None, Path]]) -> str | None:
    """The profile whose Desktop runs this session: the only data folder with records of the session's account, else
    the one holding the claude binary in the environment unless it keeps records of another account; None when
    neither says."""
    account = env.get(ACCOUNT_VARIABLE, "").strip()
    if "/" in account or account in (".", ".."):
        account = ""
    holding = [name for name, folder in folders if account and (folder / SESSIONS / account).is_dir()]
    if len(holding) == 1:
        return holding[0]
    binary = env.get(BINARY_VARIABLE)
    for name, folder in folders if binary else ():
        if _inside(binary, folder / BINARIES):
            accounts = _accounts(folder)
            # else the binary is that of the session Desktop was started from, in another profile's folder
            return name if not account or account in accounts or not (holding or accounts) else None
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


def desktop_email(env: Mapping[str, str]) -> str | None:
    """The account Desktop names in the environment of a session of its Code tab, None for any other session."""
    email = env.get(EMAIL_VARIABLE, "").strip()
    return email if "@" in email and len(email) <= 320 else None


def read_account(session_id: str, env: Mapping[str, str] | None = None, home: Path | None = None) -> Account:
    """The account of the session, ``Account('unknown')`` when nothing says; never raises."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    try:
        folders = data_folders(env, home)
        email = desktop_email(env)
        if email:  # Desktop says whose session it is
            return Account(email, running_profile(env, folders))
        found = desktop_account(session_id, folders)
        if found is not None:  # the CLI's account is not a Desktop profile's: no fallback to it
            return found
        return Account(cli_email(env, home) or UNKNOWN)
    except Exception:  # a hook never fails the session
        return Account()
