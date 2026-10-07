"""``evo-agents worker doctor [--json]``: what this machine holds or allows that a worker should not.

A worker's agents run as its user with ``bypassPermissions``, and on a machine others administer, root reads every
file of that user (docs/credentials.md). From 0.5.0 a worker needs no long-lived credential of its owner: the hub
leases each run what it needs. The doctor says what the machine still holds, as findings, each with a code, a
severity (high, medium, low), what was found and how to fix it:

- ``other-admin`` (high): another account of the admin group, ``dscl . read /Groups/admin GroupMembership`` on
  macOS, the groups sudo and wheel on Linux. Root and macOS's system accounts (``_name``) do not count.
- ``home-readable``: the home, ``~/.evo`` or ``~/github`` readable by group or other. High when every account reads
  it, medium when other members of its group do, low when only the mode of the home keeps them out.
- ``machine-token-on-worker``: ``~/.evo/hub/token``. High when the hub's whoami says its login is an admin of the
  hub, medium for a member or when the hub does not say, low when the hub refuses it (revoked or expired).
- ``gh-token-file`` (high): an ``oauth_token`` in gh's ``hosts.yml``.
- ``ssh-private-key`` (high): a private key under ``~/.ssh`` that opens without a passphrase
  (``ssh-keygen -y -P "" -f``), or that ssh-keygen refuses because group or other can read it.
- ``runtime-login-stored`` (medium): Claude Code still signed in (the keychain item ``Claude Code-credentials``,
  ``~/.claude/.credentials.json``), Codex's ``~/.codex/auth.json``.
- ``path-not-owned``: a directory on the service's PATH, or the binary of a runtime or its directory, owned by an
  account other than the user and root, or writable by group or other. High, or medium when only administrators can
  write it, since ``other-admin`` names them already.

The service's PATH is the one the installed LaunchAgent or systemd unit sets, or else PATH as ``service install``
would keep it. ``cmd_doctor`` exits 0 when no finding is high and 2 when one is. A check that cannot run, such as one
whose tool is not on PATH, is listed as not checked and changes no exit status. ``evo-agents worker service install``
runs the doctor, prints its high and medium findings as warnings, and installs all the same.

The doctor reads the machine and changes nothing. It never prints a secret: a token or a key is named by its file or
keychain item, and ``security`` is asked for the item's attributes only. Asking the hub whose ``~/.evo/hub/token`` it
is sends that token to the hub it came from, as ``evo-agents hub whoami`` does. Standard library and the core
package only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

HIGH, MEDIUM, LOW = "high", "medium", "low"
SEVERITIES = (HIGH, MEDIUM, LOW)
OTHER_ADMIN = "other-admin"
HOME_READABLE = "home-readable"
MACHINE_TOKEN = "machine-token-on-worker"
GH_TOKEN = "gh-token-file"
SSH_KEY = "ssh-private-key"
RUNTIME_LOGIN = "runtime-login-stored"
PATH_NOT_OWNED = "path-not-owned"
CODES = (OTHER_ADMIN, HOME_READABLE, MACHINE_TOKEN, GH_TOKEN, SSH_KEY, RUNTIME_LOGIN, PATH_NOT_OWNED)
EXIT_CLEAN = 0
EXIT_HIGH = 2
TOOL_TIMEOUT = 10  # seconds for dscl, security and ssh-keygen
WHOAMI_TIMEOUT = 5  # seconds for the hub to say whose ~/.evo/hub/token is
DARWIN_ADMIN_GROUP = "admin"
LINUX_ADMIN_GROUPS = ("sudo", "wheel")
READ_DIRS = (".evo", "github")  # under the home, besides the home itself
CLAUDE_KEYCHAIN_ITEM = "Claude Code-credentials"
KEYCHAIN_NOT_FOUND = 44  # what `security find-generic-password` exits with when no item matches
KEY_HEADER = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
KEY_HEAD_BYTES = 4096
MAX_SSH_FILES = 500
REFUSED_KEY = ("bad permissions", "UNPROTECTED PRIVATE KEY FILE")  # ssh-keygen on a key others can read


class Unchecked(Exception):
    """A check could not run; ``str`` says why."""


@dataclass(frozen=True)
class Group:
    name: str
    members: frozenset[str]


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    subject: str  # the account, path, host or runtime it is about
    detail: str
    fix: str

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    user: str
    home: str
    platform: str
    path: str
    admins: list[str] | None = None
    findings: list[Finding] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def add(self, code: str, severity: str, subject: str, detail: str, fix: str) -> None:
        self.findings.append(Finding(code, severity, subject, detail, fix))

    def skip(self, code: str, reason: str) -> None:
        self.skipped.append({"code": code, "reason": reason})

    def counts(self) -> dict[str, int]:
        return {severity: sum(1 for f in self.findings if f.severity == severity) for severity in SEVERITIES}

    def ordered(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (SEVERITIES.index(f.severity), CODES.index(f.code), f.subject))

    def exit_status(self) -> int:
        return EXIT_HIGH if any(f.severity == HIGH for f in self.findings) else EXIT_CLEAN

    def to_json(self) -> dict:
        return {
            "user": self.user,
            "home": self.home,
            "platform": self.platform,
            "path": self.path,
            "admins": self.admins,
            "findings": [f.to_json() for f in self.ordered()],
            "skipped": self.skipped,
            "counts": self.counts(),
        }


# The machine as the checks see it


def system_group(key: int | str) -> Group | None:
    """The group of gid or name ``key`` with its members: those it lists and the accounts whose primary group it is."""
    import grp
    import pwd

    try:
        entry = grp.getgrgid(key) if isinstance(key, int) else grp.getgrnam(key)
    except (KeyError, OverflowError):
        return None
    members = set(entry.gr_mem)
    members.update(account.pw_name for account in pwd.getpwall() if account.pw_gid == entry.gr_gid)
    return Group(entry.gr_name, frozenset(members))


def user_name(uid: int) -> str:
    import pwd

    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def hub_whoami(url: str, token: str) -> dict:
    """``GET /v1/auth/whoami`` of ``url`` as ``token``; HubError when the hub refuses or does not answer."""
    from evo_agents.hub.client import Hub

    answer = Hub(url, token, timeout=WHOAMI_TIMEOUT).call("GET", "/v1/auth/whoami")
    return answer if isinstance(answer, dict) else {}


def _service_path(env: Mapping[str, str]) -> str:
    """The PATH of the installed service, or the one ``service install`` would write now."""
    from evo_agents.worker import service

    try:
        installed = service.manager().installed_environment().get("PATH")
    except service.ServiceError:  # neither launchd nor systemd here
        installed = None
    return installed or service.service_path(env.get("PATH", ""))


@dataclass
class Machine:
    """What the checks read: the user, their home, the platform, the environment the tools run with, the service's
    PATH, and the hub and the groups as functions a test can replace."""

    home: Path
    user: str
    uid: int
    platform: str
    env: Mapping[str, str]
    path: str
    whoami: Callable[[str, str], dict] = hub_whoami
    group: Callable[[int | str], Group | None] = system_group

    @classmethod
    def here(cls, path: str | None = None) -> Machine:
        """This machine, as the user running the doctor; ``path`` replaces the service's PATH."""
        uid = os.getuid()
        env = dict(os.environ)
        return cls(
            home=Path.home(),
            user=user_name(uid),
            uid=uid,
            platform=sys.platform,
            env=env,
            path=path if path is not None else _service_path(env),
        )

    def run(self, *argv: str) -> subprocess.CompletedProcess:
        """``argv`` with the tool found on PATH of ``env``, its output captured; Unchecked when it cannot run."""
        tool = shutil.which(argv[0], path=self.env.get("PATH", ""))
        if tool is None:
            raise Unchecked(f"{argv[0]} is not on PATH")
        try:
            return subprocess.run(
                [tool, *argv[1:]],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=TOOL_TIMEOUT,
                env=dict(self.env),
            )
        except subprocess.TimeoutExpired:
            raise Unchecked(f"{argv[0]} did not finish within {TOOL_TIMEOUT} seconds") from None
        except OSError as exc:
            raise Unchecked(f"{argv[0]} did not run: {exc.strerror or exc}") from None

    def shown(self, path: Path | str) -> str:
        """``path`` with the home written ``~``."""
        text = str(path)
        home = str(self.home)
        if text == home:
            return "~"
        return "~" + text[len(home) :] if text.startswith(home + os.sep) else text


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "no output")


# other-admin


def admins(machine: Machine) -> list[str]:
    """The accounts of the admin group: macOS's admin as dscl reads it, Linux's sudo and wheel."""
    if machine.platform == "darwin":
        proc = machine.run("dscl", ".", "read", f"/Groups/{DARWIN_ADMIN_GROUP}", "GroupMembership")
        said = f"{proc.stdout}\n{proc.stderr}"
        if "No such key" in said:  # a group without members
            return []
        if proc.returncode != 0:
            raise Unchecked(f"dscl exited {proc.returncode}: {_first_line(proc.stderr or proc.stdout)}")
        _, sep, rest = proc.stdout.partition("GroupMembership:")
        if not sep:
            raise Unchecked(f"dscl printed no GroupMembership: {_first_line(proc.stdout)}")
        return list(dict.fromkeys(rest.split()))
    found: set[str] = set()
    for name in LINUX_ADMIN_GROUPS:
        group = machine.group(name)
        if group is not None:
            found.update(group.members)
    return sorted(found)


def _system_account(name: str) -> bool:
    return name == "root" or name.startswith("_")


def check_other_admin(machine: Machine, report: Report, admin_list: list[str]) -> None:
    others = [name for name in admin_list if name != machine.user and not _system_account(name)]
    me = "an administrator too" if machine.user in admin_list else "not one"
    for name in others:
        if machine.platform == "darwin":
            groups = [DARWIN_ADMIN_GROUP]
            commands = [f"`sudo dseditgroup -o edit -d {name} -t user {DARWIN_ADMIN_GROUP}`"]
        else:
            found = {key: machine.group(key) for key in LINUX_ADMIN_GROUPS}
            groups = [key for key, group in found.items() if group is not None and name in group.members]
            commands = [f"`sudo gpasswd -d {name} {key}`" for key in groups]
        report.add(
            OTHER_ADMIN,
            HIGH,
            name,
            f"{name} administers this machine (group {' and '.join(groups)}); {machine.user} is {me}. With sudo, "
            f"{name} reads every file of {machine.user}, the keychain once it is unlocked, and the memory of the "
            "daemon and its agents.",
            f"Take {name} out of the group ({', '.join(commands)}), or run the worker where only you administer. "
            "Where that cannot be, keep no long-lived credential here (the other findings) and count every lease of "
            "a run as readable by them while the run holds it.",
        )


# home-readable


def _reaches(group: Group | None, machine: Machine) -> bool:
    """Whether a group's read bit lets an account other than the user and root in."""
    if group is None:
        return True
    return any(name != machine.user and name != "root" for name in group.members)


def check_home_readable(machine: Machine, report: Report, admin_list) -> None:
    try:
        home_mode = stat.S_IMODE(machine.home.stat().st_mode)
    except OSError as exc:
        raise Unchecked(f"cannot read the mode of {machine.home}: {exc.strerror or exc}") from None
    for path in (machine.home, *(machine.home / name for name in READ_DIRS)):
        try:
            info = path.stat()
        except OSError:
            continue
        mode = stat.S_IMODE(info.st_mode)
        group = machine.group(info.st_gid) if mode & stat.S_IRGRP else None
        group_name = group.name if group is not None else f"gid {info.st_gid}"
        readers = []  # who reads it once past the home
        if mode & stat.S_IROTH:
            readers.append("other")
        if mode & stat.S_IRGRP and _reaches(group, machine):
            readers.append("group")
        if not readers:
            continue
        if path != machine.home:
            past = {"other": stat.S_IXOTH, "group": stat.S_IXGRP}
            reached = [who for who in readers if home_mode & past[who]]
        else:
            reached = readers
        shown = machine.shown(path)
        whom = {"other": "every account of this machine", "group": f"the other accounts of group {group_name}"}
        fix = (
            f"`chmod {'700' if path == machine.home else '-R go-rwx'} {shown}`, and `umask 077` in the shell's "
            "profile so what you create next is yours alone (the service starts the daemon with umask 077 already)."
        )
        if not reached:
            report.add(
                HOME_READABLE,
                LOW,
                shown,
                f"{shown} is mode {mode:04o}, which lets {' and '.join(whom[who] for who in readers)} read it; only "
                f"the mode of ~ ({home_mode:04o}) keeps them out.",
                fix,
            )
            continue
        report.add(
            HOME_READABLE,
            HIGH if "other" in reached else MEDIUM,
            shown,
            f"{shown} is mode {mode:04o}: {' and '.join(whom[who] for who in reached)} can read it, and what in it "
            "is not closed on its own.",
            fix,
        )


# machine-token-on-worker


def check_machine_token(machine: Machine, report: Report, admin_list) -> None:
    from evo_agents.hub.client import HubError

    directory = machine.home / ".evo" / "hub"
    path = directory / "token"
    shown = machine.shown(path)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise Unchecked(f"cannot read {shown}: {exc.strerror or exc}") from None
    fix = (
        "`evo-agents hub logout` here: it revokes the token on the hub and deletes it from this machine. A worker "
        "needs only its worker token; drain or revoke it from the web, or from another machine signed in to the hub."
    )
    try:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = None
    url = config.get("url") if isinstance(config, dict) else None
    if not token:
        report.add(MACHINE_TOKEN, LOW, shown, f"{shown} is there, empty.", fix)
        return
    if not isinstance(url, str) or not url:
        report.add(
            MACHINE_TOKEN,
            MEDIUM,
            shown,
            f"{shown} holds a machine token of the hub, without the hub's URL beside it, so whose it is was not asked.",
            fix,
        )
        return
    try:
        me = machine.whoami(url, token)
    except HubError as exc:
        if exc.status == 401:
            report.add(
                MACHINE_TOKEN,
                LOW,
                shown,
                f"{shown} holds a machine token that {url} refuses (revoked or expired): the file is left over.",
                fix,
            )
        else:
            report.add(
                MACHINE_TOKEN,
                MEDIUM,
                shown,
                f"{shown} holds a machine token of {url}, which did not say whose it is ({exc}).",
                fix,
            )
        return
    login = me.get("login") if isinstance(me.get("login"), str) else "an unknown login"
    admin = me.get("admin") is True
    token_info = me.get("token") if isinstance(me.get("token"), dict) else {}
    grants = me.get("grants") if isinstance(me.get("grants"), list) else []
    facts = [f"token {token_info['id']}"] if token_info.get("id") is not None else []
    if token_info.get("expires_at"):
        facts.append(f"expires {token_info['expires_at']}")
    facts.append(f"{len(grants)} grant(s)")
    report.add(
        MACHINE_TOKEN,
        HIGH if admin else MEDIUM,
        shown,
        f"{shown} is a machine token of {login}{', an admin of the hub,' if admin else ''} on {url} "
        f"({', '.join(facts)}): whoever reads it acts as {login} on the hub until it expires.",
        fix,
    )


# gh-token-file


def gh_hosts(machine: Machine) -> Path:
    """gh's hosts.yml: in GH_CONFIG_DIR, else XDG_CONFIG_HOME/gh, else ~/.config/gh."""
    configured = machine.env.get("GH_CONFIG_DIR") or ""
    if configured:
        return Path(configured).expanduser() / "hosts.yml"
    xdg = machine.env.get("XDG_CONFIG_HOME") or ""
    base = Path(xdg) if os.path.isabs(xdg) else machine.home / ".config"
    return base / "gh" / "hosts.yml"


def _gh_tokens(text: str) -> dict[str, list[str]]:
    """Each host of hosts.yml that holds an oauth_token, with the logins it holds one for; never the token."""
    import yaml

    def has_token(entry) -> bool:
        return isinstance(entry, dict) and isinstance(entry.get("oauth_token"), str) and bool(entry["oauth_token"])

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):  # gh could not read it either, but the token is there all the same
        return {"": []} if re.search(r"^\s*oauth_token:\s*\S", text, re.M) else {}
    found: dict[str, list[str]] = {}
    for host, entry in data.items():
        if not isinstance(entry, dict):
            continue
        logins = []
        users = entry.get("users")
        if isinstance(users, dict):
            logins.extend(str(login) for login, item in users.items() if has_token(item))
        if has_token(entry):
            user = entry.get("user")
            if isinstance(user, str) and user and user not in logins:
                logins.insert(0, user)
            elif not logins:
                logins.append("an unnamed login")
        if logins:
            found[str(host)] = logins
    return found


def check_gh_token(machine: Machine, report: Report, admin_list) -> None:
    path = gh_hosts(machine)
    shown = machine.shown(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    except OSError as exc:
        raise Unchecked(f"cannot read {shown}: {exc.strerror or exc}") from None
    for host, logins in _gh_tokens(text).items():
        where = f"on {host}" if host else "that YAML cannot place under a host"
        report.add(
            GH_TOKEN,
            HIGH,
            host or shown,
            f"{shown} holds the gh token of {', '.join(logins) or 'a login'} {where}, in plain text: whoever reads it "
            "works as that login on every repository it reaches, until the token is revoked.",
            "Revoke it, then sign gh out here: GitHub revokes a token sent to POST "
            "https://api.github.com/credentials/revoke without a sign-in (pipe `gh auth token` into the body, never "
            f"onto a command line), then `gh auth logout --hostname {host or 'HOST'}`. Runs push with a GitHub App "
            "token the hub leases them.",
        )


# ssh-private-key


def _private_keys(root: Path) -> list[Path]:
    found = []
    seen = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".pub"):
                continue
            seen += 1
            if seen > MAX_SSH_FILES:
                return found
            path = Path(directory) / name
            try:
                if not path.is_file():
                    continue
                with open(path, "rb") as handle:
                    head = handle.read(KEY_HEAD_BYTES)
            except OSError:
                continue
            if KEY_HEADER.search(head):
                found.append(path)
    return found


def check_ssh_keys(machine: Machine, report: Report, admin_list) -> None:
    root = machine.home / ".ssh"
    if not root.is_dir():
        return
    keys = _private_keys(root)
    for key in keys:
        proc = machine.run("ssh-keygen", "-y", "-P", "", "-f", str(key))  # stdout, the public key, is dropped
        shown = machine.shown(key)
        fix = (
            "Delete it here, and its public key wherever it is authorized (GitHub, GitLab, servers), or give it a "
            f"passphrase: `ssh-keygen -p -f {shown}`. Runs fetch and push over https with the tokens the hub leases."
        )
        if proc.returncode == 0:
            report.add(
                SSH_KEY,
                HIGH,
                shown,
                f"{shown} is a private key that opens without a passphrase: whoever reads it signs in wherever its "
                "public key is authorized.",
                fix,
            )
        elif any(marker in proc.stderr for marker in REFUSED_KEY):
            report.add(
                SSH_KEY,
                HIGH,
                shown,
                f"{shown} is a private key that group or other can read; ssh-keygen refuses to open it as it is, so "
                "whether it has a passphrase was not checked.",
                f"`chmod 600 {shown}`, then run the doctor again. " + fix,
            )


# runtime-login-stored


def check_runtime_logins(machine: Machine, report: Report, admin_list) -> None:
    configured = machine.env.get("CLAUDE_CONFIG_DIR") or ""
    claude_dir = Path(configured) if os.path.isabs(configured) else machine.home / ".claude"
    places = []
    credentials = claude_dir / ".credentials.json"
    if credentials.is_file():
        places.append(machine.shown(credentials))
    if machine.platform == "darwin":
        try:  # the item's attributes only: neither -w nor -g, which print the secret
            proc = machine.run("security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_ITEM)
        except Unchecked as exc:
            report.skip(RUNTIME_LOGIN, f"the keychain of Claude Code: {exc}")
        else:
            if proc.returncode == 0:
                places.append(f"the keychain item {CLAUDE_KEYCHAIN_ITEM}")
            elif proc.returncode != KEYCHAIN_NOT_FOUND:
                report.skip(RUNTIME_LOGIN, f"the keychain of Claude Code: security exited {proc.returncode}")
    if places:
        report.add(
            RUNTIME_LOGIN,
            MEDIUM,
            "claude-code",
            f"Claude Code is signed in here ({', '.join(places)}): every agent of a run can read that login, which "
            "outlives the run.",
            "Sign Claude Code out here (/logout in a claude session) and give runs CLAUDE_CODE_OAUTH_TOKEN from a "
            "secret of kind env (`claude setup-token` on your own machine): the daemon hands it to a run's agent for "
            "that run only.",
        )
    configured = machine.env.get("CODEX_HOME") or ""
    codex = (Path(configured) if os.path.isabs(configured) else machine.home / ".codex") / "auth.json"
    if codex.is_file():
        report.add(
            RUNTIME_LOGIN,
            MEDIUM,
            "codex",
            f"Codex is signed in here ({machine.shown(codex)}): every agent of a run can read that login, which "
            "outlives the run.",
            "`codex logout` here, and give runs the key Codex needs from a secret of kind env.",
        )


# path-not-owned


def _writers(info: os.stat_result, machine: Machine, admin_list: list[str] | None) -> list[tuple[str, bool]]:
    """Who besides the user and root can write a file or directory of ``info``, each with whether only
    administrators are meant."""
    known = set(admin_list or ())
    writers = []
    if info.st_uid not in (machine.uid, 0):
        owner = user_name(info.st_uid)
        writers.append((f"owned by {owner}", owner in known))
    if info.st_mode & stat.S_IWGRP:
        group = machine.group(info.st_gid)
        if group is None:
            writers.append((f"writable by gid {info.st_gid}", False))
        else:
            admin_group = machine.platform == "darwin" and group.name == DARWIN_ADMIN_GROUP
            members = set(admin_list) if admin_group and admin_list is not None else set(group.members)
            others = sorted(name for name in members if name != machine.user and name != "root")
            if others:
                shown = [name for name in others if not name.startswith("_")] or others
                writers.append((f"writable by group {group.name} ({', '.join(shown)})", set(others) <= known))
    if info.st_mode & stat.S_IWOTH:
        writers.append(("writable by every account", False))
    return writers


def check_path(machine: Machine, report: Report, admin_list) -> None:
    from evo_agents.worker.service import RUNTIME_COMMANDS

    checked: set[str] = set()

    def look(path: str, what: str) -> None:
        real = os.path.realpath(path)
        if real in checked:
            return
        checked.add(real)
        try:
            info = os.stat(real)
        except OSError:
            return
        if path != real and os.path.isfile(real):  # a binary through a link: the file it names is what runs
            path, what = real, f"{what} through {machine.shown(path)}"
        writers = _writers(info, machine, admin_list)
        if not writers:
            return
        only_admins = all(admin for _, admin in writers)
        shown = machine.shown(path)
        report.add(
            PATH_NOT_OWNED,
            MEDIUM if only_admins else HIGH,
            shown,
            f"{shown}, {what}, is {' and '.join(text for text, _ in writers)}: whoever writes it runs code as "
            f"{machine.user} in every run"
            + (", and only administrators can, whom other-admin names already." if only_admins else "."),
            f"Make it {machine.user}'s alone (`sudo chown {machine.user} {shown}`, `chmod go-w {shown}`), or install "
            "the runtimes where only you write, such as ~/.local/bin, put that first on PATH, and run `evo-agents "
            "worker service install` again.",
        )

    for entry in machine.path.split(os.pathsep):
        if entry and os.path.isabs(entry) and os.path.isdir(entry):
            look(entry, "a directory on the service's PATH")
    for name in RUNTIME_COMMANDS:
        found = shutil.which(name, path=machine.path)
        if found is None:
            continue
        real = os.path.realpath(found)
        look(found, f"the {name} the service runs")
        look(os.path.dirname(real), f"the directory of the {name} the service runs")


CHECKS = (
    (HOME_READABLE, check_home_readable),
    (MACHINE_TOKEN, check_machine_token),
    (GH_TOKEN, check_gh_token),
    (SSH_KEY, check_ssh_keys),
    (RUNTIME_LOGIN, check_runtime_logins),
    (PATH_NOT_OWNED, check_path),
)


def examine(machine: Machine) -> Report:
    """Every check on ``machine``; one that cannot run is in ``skipped``."""
    report = Report(user=machine.user, home=str(machine.home), platform=machine.platform, path=machine.path)
    try:
        report.admins = admins(machine)
    except Unchecked as exc:
        report.skip(OTHER_ADMIN, str(exc))
    else:
        check_other_admin(machine, report, report.admins)
    for code, check in CHECKS:
        try:
            check(machine, report, report.admins)
        except Unchecked as exc:
            report.skip(code, str(exc))
    return report


def warnings(report: Report) -> list[str]:
    """The lines ``service install`` prints of ``report``: its high and medium findings."""
    shown = [f for f in report.ordered() if f.severity != LOW]
    if not shown:
        return []
    lines = [f"warning: doctor: {f.severity} {f.code}: {f.detail}" for f in shown]
    counts = report.counts()
    lines.append(
        f"warning: doctor: {counts[HIGH]} high, {counts[MEDIUM]} medium; `evo-agents worker doctor` says how to fix "
        "each. The service is installed all the same."
    )
    return lines


def print_report(report: Report) -> None:
    print(f"This machine as a worker, for {report.user} ({report.home}):")
    for finding in report.ordered():
        print(f"  {finding.severity} {finding.code}: {finding.detail}")
        print(f"    fix: {finding.fix}")
    for item in report.skipped:
        print(f"  not checked: {item['code']}: {item['reason']}")
    counts = report.counts()
    if not report.findings:
        print("No finding.")
        return
    print(
        f"{counts[HIGH]} high, {counts[MEDIUM]} medium, {counts[LOW]} low"
        + (": exit 2 while a finding is high." if counts[HIGH] else ".")
    )


def cmd_doctor(args) -> int:
    report = examine(Machine.here())
    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return report.exit_status()


def add_parser(wsub) -> None:
    parser = wsub.add_parser(
        "doctor",
        help="what this machine holds that a worker should not: other administrators, a readable home, long-lived "
        "credentials, a PATH others write; exits 2 when a finding is high",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.set_defaults(func=cmd_doctor)
