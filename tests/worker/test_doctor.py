"""``evo-agents worker doctor`` (``evo_agents.worker.doctor``): each finding's code and severity, the exit status, and
the warnings of ``evo-agents worker service install``.

The checks step 11 of the worker-credentials plan names: other-admin from dscl (or the groups sudo and wheel on
Linux), home-readable, machine-token-on-worker by the hub's whoami, gh-token-file, ssh-private-key by
``ssh-keygen -y -P "" -f``, runtime-login-stored (Claude Code's keychain item through ``security``, its credentials
file, Codex's auth.json) and path-not-owned, then exit 2 while a finding is high. Every HOME is a temporary one, and
dscl, security and ssh-keygen are fakes on PATH that log their arguments; the hub's whoami and the groups are
functions of the test. Nothing reads this machine's home, keychain or admin group, and no secret a test writes shows
up in what the doctor prints.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.hub.client import HubError, Unreachable
from evo_agents.worker import doctor, service
from evo_agents.worker.doctor import HIGH, LOW, MEDIUM, Finding, Group, Machine, Report
from evo_agents.worker.home import WorkerConfig, WorkerHome
from tests.worker.test_service import FakeLaunchctl

ME = doctor.user_name(os.getuid())
ADMINS = f"root _mbsetupuser {ME}"
# The armour of a private key, split so that no secret scanner takes this file for one; the body is no key.
ARMOUR = "OPENSSH " + "PRIVATE KEY"
PRIVATE_KEY = f"-----BEGIN {ARMOUR}-----\nnot a key\n-----END {ARMOUR}-----\n"


def sample(prefix: str) -> str:
    """A value drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


class Tools:
    """A directory of fake tools: dscl, security and ssh-keygen as /bin/sh scripts that log their arguments."""

    def __init__(self, root: Path):
        self.bin = root / "bin"
        self.bin.mkdir()
        self.logs = root / "logs"
        self.logs.mkdir()

    def add(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "$*" >> "{self.logs / name}"\n{body}\n', encoding="utf-8")
        path.chmod(0o755)

    def calls(self, name: str) -> list[str]:
        path = self.logs / name
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def dscl(self, members: str = ADMINS) -> None:
        self.add("dscl", f'echo "GroupMembership: {members}"')

    def security(self, found: bool = False) -> None:
        item = '\n  *"Claude Code-credentials"*) echo "keychain: login.keychain-db"; exit 0 ;;' if found else ""
        self.add("security", f'case "$*" in{item}\n  *) exit 44 ;;\nesac')

    def ssh_keygen(self) -> None:
        """Opens a key whose name holds "plain", refuses one whose name holds "open" for its mode, and asks the
        passphrase of any other. It reads the file's name, not its path: under pytest-xdist every tmp_path holds
        "popen-gw<n>"."""
        self.add(
            "ssh-keygen",
            'for last; do :; done\ncase "${last##*/}" in\n'
            '  *plain*) echo "ssh-ed25519 AAAAC3Nz fake"; exit 0 ;;\n'
            '  *open*) echo "Load key \\"$last\\": bad permissions" >&2; exit 255 ;;\n'
            '  *) echo "Load key \\"$last\\": incorrect passphrase supplied to decrypt private key" >&2; exit 255 ;;\n'
            "esac",
        )


def no_hub(url: str, token: str) -> dict:
    raise AssertionError("the doctor asks the hub only when ~/.evo/hub/token is there")


def only_me(key) -> Group:
    return Group("staff", frozenset({ME}))


@pytest.fixture
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    path.chmod(0o700)
    return path


@pytest.fixture
def tools(tmp_path) -> Tools:
    found = Tools(tmp_path)
    found.dscl()
    found.security()
    found.ssh_keygen()
    return found


def machine_of(home: Path, tools: Tools, **changes) -> Machine:
    values = {
        "home": home,
        "user": ME,
        "uid": os.getuid(),
        "platform": "darwin",
        "env": {"PATH": str(tools.bin)},
        "path": str(tools.bin),
        "whoami": no_hub,
        "group": only_me,
    }
    values.update(changes)
    return Machine(**values)


def found(report: Report, code: str) -> dict[str, Finding]:
    return {finding.subject: finding for finding in report.findings if finding.code == code}


# other-admin


def test_other_admin_reads_the_admin_group_with_dscl_without_root_and_system_accounts(home, tools):
    tools.dscl(f"root _mbsetupuser m1 {ME}")
    report = doctor.examine(machine_of(home, tools))
    assert tools.calls("dscl") == [". read /Groups/admin GroupMembership"]
    assert report.admins == ["root", "_mbsetupuser", "m1", ME]
    admins = found(report, "other-admin")
    assert list(admins) == ["m1"] and admins["m1"].severity == HIGH
    assert f"m1 administers this machine (group admin); {ME} is an administrator too" in admins["m1"].detail
    assert "dseditgroup -o edit -d m1 -t user admin" in admins["m1"].fix
    assert report.exit_status() == 2

    tools.dscl(ADMINS)
    report = doctor.examine(machine_of(home, tools))
    assert report.findings == [] and report.skipped == [] and report.exit_status() == 0


def test_other_admin_on_linux_is_a_member_of_sudo_or_wheel(home, tools):
    groups = {"sudo": Group("sudo", frozenset({"root", "alice", ME})), "wheel": Group("wheel", frozenset({"bob"}))}

    def group(key):
        return groups.get(key) if isinstance(key, str) else only_me(key)

    report = doctor.examine(machine_of(home, tools, platform="linux", group=group))
    admins = found(report, "other-admin")
    assert sorted(admins) == ["alice", "bob"] and {f.severity for f in admins.values()} == {HIGH}
    assert "(group sudo); " in admins["alice"].detail and "`sudo gpasswd -d alice sudo`" in admins["alice"].fix
    assert "(group wheel); " in admins["bob"].detail and "`sudo gpasswd -d bob wheel`" in admins["bob"].fix
    assert tools.calls("dscl") == [] and tools.calls("security") == [], "Linux has no dscl and no keychain"


def test_a_check_whose_tool_is_missing_is_not_checked_and_changes_no_exit_status(home, tools):
    for name in ("dscl", "ssh-keygen"):
        (tools.bin / name).unlink()
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_plain").write_text(PRIVATE_KEY, encoding="utf-8")
    report = doctor.examine(machine_of(home, tools))
    assert report.skipped == [
        {"code": "other-admin", "reason": "dscl is not on PATH"},
        {"code": "ssh-private-key", "reason": "ssh-keygen is not on PATH"},
    ]
    assert report.admins is None and report.findings == [] and report.exit_status() == 0


# home-readable


def test_home_readable_is_high_for_every_account_medium_for_the_group_low_behind_a_closed_home(home, tools):
    for name in (".evo", "github"):
        (home / name).mkdir()
    home.chmod(0o755)
    (home / ".evo").chmod(0o755)
    (home / "github").chmod(0o750)
    with_alice = lambda key: Group("staff", frozenset({ME, "alice"}))  # noqa: E731
    report = doctor.examine(machine_of(home, tools, group=with_alice))
    readable = found(report, "home-readable")
    assert {subject: f.severity for subject, f in readable.items()} == {"~": HIGH, "~/.evo": HIGH, "~/github": MEDIUM}
    assert "every account of this machine" in readable["~/.evo"].detail
    assert "the other accounts of group staff can read it" in readable["~/github"].detail
    assert readable["~"].fix.startswith("`chmod 700 ~`") and "chmod -R go-rwx ~/.evo" in readable["~/.evo"].fix

    # A group of the user alone reads nothing of someone else's.
    report = doctor.examine(machine_of(home, tools))
    assert {s: f.severity for s, f in found(report, "home-readable").items()} == {"~": HIGH, "~/.evo": HIGH}

    home.chmod(0o700)
    report = doctor.examine(machine_of(home, tools, group=with_alice))
    readable = found(report, "home-readable")
    assert {subject: f.severity for subject, f in readable.items()} == {"~/.evo": LOW, "~/github": LOW}
    assert "only the mode of ~ (0700) keeps them out" in readable["~/.evo"].detail
    assert report.exit_status() == 0

    for name in (".evo", "github"):
        (home / name).chmod(0o700)
    assert found(doctor.examine(machine_of(home, tools, group=with_alice)), "home-readable") == {}


# machine-token-on-worker


def hub_token(home: Path, url: str | None = "https://hub.example.org") -> str:
    token = sample("evh_")
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    if url is not None:
        (directory / "config.json").write_text(json.dumps({"url": url, "login": "octo"}), encoding="utf-8")
    return token


def test_a_machine_token_is_high_for_an_admin_of_the_hub_medium_for_a_member_low_once_refused(home, tools):
    token = hub_token(home)
    asked = []

    def whoami_as(answer):
        def whoami(url, given):
            asked.append((url, given))
            if isinstance(answer, Exception):
                raise answer
            return answer

        return whoami

    admin = {"login": "octo", "admin": True, "token": {"id": 13, "expires_at": "2027-01-04T00:00:00Z"}, "grants": [{}]}
    report = doctor.examine(machine_of(home, tools, whoami=whoami_as(admin)))
    finding = found(report, "machine-token-on-worker")["~/.evo/hub/token"]
    assert asked == [("https://hub.example.org", token)]
    assert finding.severity == HIGH and report.exit_status() == 2
    assert "a machine token of octo, an admin of the hub, on https://hub.example.org" in finding.detail
    assert "token 13, expires 2027-01-04T00:00:00Z, 1 grant(s)" in finding.detail
    assert "`evo-agents hub logout`" in finding.fix
    assert token not in json.dumps(report.to_json())

    member = {**admin, "admin": False}
    cases = [
        (member, MEDIUM, "a machine token of octo on https://hub.example.org"),
        (HubError("the token was revoked", 401), LOW, "that https://hub.example.org refuses (revoked or expired)"),
        (Unreachable("cannot reach https://hub.example.org: refused"), MEDIUM, "which did not say whose it is"),
    ]
    for answer, severity, said in cases:
        report = doctor.examine(machine_of(home, tools, whoami=whoami_as(answer)))
        finding = found(report, "machine-token-on-worker")["~/.evo/hub/token"]
        assert finding.severity == severity and said in finding.detail, answer
        assert token not in json.dumps(report.to_json())

    (home / ".evo" / "hub" / "config.json").unlink()
    report = doctor.examine(machine_of(home, tools))  # no URL: the hub is not asked
    finding = found(report, "machine-token-on-worker")["~/.evo/hub/token"]
    assert finding.severity == MEDIUM and "without the hub's URL beside it" in finding.detail


# gh-token-file


def test_gh_token_file_names_the_host_and_login_of_each_oauth_token_never_the_token(home, tools, tmp_path):
    token = sample("gho_")
    hosts = (
        "github.com:\n"
        "    users:\n"
        "        octo:\n"
        f"            oauth_token: {token}\n"
        "    git_protocol: https\n"
        f"    oauth_token: {token}\n"
        "    user: octo\n"
        "gitlab.example.org:\n"
        "    git_protocol: ssh\n"
        "    user: someone\n"
    )
    config = home / ".config" / "gh"
    config.mkdir(parents=True)
    (config / "hosts.yml").write_text(hosts, encoding="utf-8")
    report = doctor.examine(machine_of(home, tools))
    tokens = found(report, "gh-token-file")
    assert list(tokens) == ["github.com"] and tokens["github.com"].severity == HIGH
    assert "~/.config/gh/hosts.yml holds the gh token of octo on github.com" in tokens["github.com"].detail
    assert "gh auth logout --hostname github.com" in tokens["github.com"].fix
    assert token not in json.dumps(report.to_json())

    # A token kept in the system's keyring leaves hosts.yml without one.
    elsewhere = tmp_path / "gh"
    elsewhere.mkdir()
    (elsewhere / "hosts.yml").write_text("github.com:\n    git_protocol: https\n    user: octo\n", encoding="utf-8")
    env = {"PATH": str(tools.bin), "GH_CONFIG_DIR": str(elsewhere)}
    assert found(doctor.examine(machine_of(home, tools, env=env)), "gh-token-file") == {}


# ssh-private-key


def test_ssh_private_key_is_one_that_opens_without_a_passphrase_or_that_others_can_read(home, tools):
    ssh = home / ".ssh"
    (ssh / "old").mkdir(parents=True)
    for name in ("id_plain", "id_locked", "id_open", "old/deploy_plain"):
        (ssh / name).write_text(PRIVATE_KEY, encoding="utf-8")
    (ssh / "id_plain.pub").write_text("ssh-ed25519 AAAAC3Nz octo\n", encoding="utf-8")
    for name in ("known_hosts", "config", "authorized_keys"):
        (ssh / name).write_text("github.com ssh-ed25519 AAAAC3Nz\n", encoding="utf-8")
    report = doctor.examine(machine_of(home, tools))
    keys = found(report, "ssh-private-key")
    assert sorted(keys) == ["~/.ssh/id_open", "~/.ssh/id_plain", "~/.ssh/old/deploy_plain"]
    assert {f.severity for f in keys.values()} == {HIGH} and report.exit_status() == 2
    assert "opens without a passphrase" in keys["~/.ssh/id_plain"].detail
    assert "ssh-keygen -p -f ~/.ssh/id_plain" in keys["~/.ssh/id_plain"].fix
    assert "group or other can read" in keys["~/.ssh/id_open"].detail
    assert keys["~/.ssh/id_open"].fix.startswith("`chmod 600 ~/.ssh/id_open`")
    assert sorted(tools.calls("ssh-keygen")) == sorted(
        f"-y -P  -f {ssh / name}" for name in ("id_locked", "id_open", "id_plain", "old/deploy_plain")
    ), "ssh-keygen sees the private keys only, with an empty passphrase"
    assert "AAAAC3Nz" not in json.dumps(report.to_json()), "what ssh-keygen prints is dropped"


# runtime-login-stored


def test_runtime_login_stored_is_claude_codes_keychain_item_or_credentials_file_and_codexs_auth(home, tools):
    tools.security(found=True)
    report = doctor.examine(machine_of(home, tools))
    logins = found(report, "runtime-login-stored")
    assert list(logins) == ["claude-code"] and logins["claude-code"].severity == MEDIUM
    assert "Claude Code is signed in here (the keychain item Claude Code-credentials)" in logins["claude-code"].detail
    assert tools.calls("security") == ["find-generic-password -s Claude Code-credentials"], "never -w or -g"
    assert report.exit_status() == 0, "a stored login is medium"

    secret = sample("sk-ant-oat01-")
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"accessToken": secret}), encoding="utf-8")
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": secret}), encoding="utf-8")
    tools.security(found=False)
    report = doctor.examine(machine_of(home, tools, platform="linux"))
    logins = found(report, "runtime-login-stored")
    assert sorted(logins) == ["claude-code", "codex"] and {f.severity for f in logins.values()} == {MEDIUM}
    assert "(~/.claude/.credentials.json)" in logins["claude-code"].detail
    assert "(~/.codex/auth.json)" in logins["codex"].detail
    assert len(tools.calls("security")) == 1, "Linux has no keychain to ask"
    assert secret not in json.dumps(report.to_json())

    for path in (home / ".claude" / ".credentials.json", home / ".codex" / "auth.json"):
        path.unlink()
    assert found(doctor.examine(machine_of(home, tools)), "runtime-login-stored") == {}


# path-not-owned


def test_path_not_owned_is_a_directory_or_runtime_others_can_write(home, tools, tmp_path):
    everyone = tmp_path / "everyone"
    everyone.mkdir()
    everyone.chmod(0o777)
    grouped = tmp_path / "grouped"
    grouped.mkdir()
    grouped.chmod(0o775)
    runtimes = tmp_path / "runtimes"
    (runtimes / "versions").mkdir(parents=True)
    claude = runtimes / "versions" / "claude-2.0"
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    claude.chmod(0o777)
    (tools.bin / "claude").symlink_to(claude)
    path = os.pathsep.join([str(tools.bin), str(everyone), str(grouped), "/usr/bin", "relative/bin", "/no/such/dir"])

    with_alice = lambda key: Group("staff", frozenset({ME, "root", "alice"}))  # noqa: E731
    report = doctor.examine(machine_of(home, tools, path=path, group=with_alice))
    writable = found(report, "path-not-owned")
    binary = os.path.realpath(claude)
    assert sorted(writable) == sorted([str(everyone), str(grouped), binary]), "root's /usr/bin passes"
    assert {f.severity for f in writable.values()} == {HIGH}
    assert "a directory on the service's PATH, is writable by group staff (alice) and writable by every account" in (
        writable[str(everyone)].detail
    )
    assert "is writable by group staff (alice)" in writable[str(grouped)].detail
    assert f"the claude the service runs through {tools.bin / 'claude'}" in writable[binary].detail
    assert f"`sudo chown {ME} {grouped}`" in writable[str(grouped)].fix

    # A group of administrators only: other-admin names them already.
    admin_group = lambda key: Group("admin", frozenset())  # noqa: E731
    tools.dscl(f"root m1 {ME}")
    report = doctor.examine(machine_of(home, tools, path=str(grouped), group=admin_group))
    assert {s: f.severity for s, f in found(report, "path-not-owned").items()} == {str(grouped): MEDIUM}
    detail = found(report, "path-not-owned")[str(grouped)].detail
    assert (
        "writable by group admin (m1)" in detail and "only administrators can, whom other-admin names already" in detail
    )

    # A directory of another account: high, or medium when that account is an administrator.
    grouped.chmod(0o755)
    as_worker = {"user": "worker", "uid": os.getuid() + 4242, "path": str(grouped)}
    report = doctor.examine(machine_of(home, tools, **as_worker))
    finding = found(report, "path-not-owned")[str(grouped)]
    assert finding.severity == MEDIUM and f"is owned by {ME}" in finding.detail
    tools.dscl("root m1")
    finding = found(doctor.examine(machine_of(home, tools, **as_worker)), "path-not-owned")[str(grouped)]
    assert finding.severity == HIGH


# The command


@pytest.fixture
def here(home, tools, monkeypatch):
    """This machine as ``Machine.here`` sees it: HOME and PATH of the test, macOS, no system directory added."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tools.bin))
    for name in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "SYSTEM_PATH", ())
    return home


def test_doctor_prints_json_and_exits_0_without_a_high_finding_and_2_with_one(here, tools, capsys):
    assert main(["worker", "doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "user": ME,
        "home": str(here),
        "platform": "darwin",
        "path": str(tools.bin),
        "admins": ["root", "_mbsetupuser", ME],
        "findings": [],
        "skipped": [],
        "counts": {"high": 0, "medium": 0, "low": 0},
    }
    assert main(["worker", "doctor"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"This machine as a worker, for {ME} ({here}):", "No finding."]

    tools.dscl(f"root m1 {ME}")
    (here / ".claude").mkdir()
    (here / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    assert main(["worker", "doctor", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert [(f["severity"], f["code"], f["subject"]) for f in report["findings"]] == [
        ("high", "other-admin", "m1"),
        ("medium", "runtime-login-stored", "claude-code"),
    ]
    assert set(report["findings"][0]) == {"code", "severity", "subject", "detail", "fix"}
    assert report["counts"] == {"high": 1, "medium": 1, "low": 0}
    assert main(["worker", "doctor"]) == 2
    out = capsys.readouterr().out
    assert "  high other-admin: m1 administers this machine (group admin)" in out
    assert "    fix: Take m1 out of the group" in out
    assert out.splitlines()[-1] == "1 high, 1 medium, 0 low: exit 2 while a finding is high."


def test_the_service_path_is_the_one_the_installed_service_sets(here, tools, tmp_path):
    installed = tmp_path / "installed-bin"
    installed.mkdir()
    plist = here / "Library" / "LaunchAgents" / f"{service.LABEL}.plist"
    plist.parent.mkdir(parents=True)
    spec = service.Spec(program=("/opt/evo-agents",), path=str(installed), home=here / ".evo" / "worker")
    plist.write_text(service.Launchd().render(spec), encoding="utf-8")
    assert Machine.here().path == str(installed)
    assert Machine.here(path="/given").path == "/given"


# `service install`


@pytest.fixture
def installing(here, monkeypatch):
    """A worker of this HOME and a fake launchctl, as tests/worker/test_service.py has them."""
    monkeypatch.setattr(sys, "argv", ["/opt/tools/bin/evo-agents"])
    monkeypatch.setattr(service, "missing_extra", lambda: None)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    monkeypatch.delenv("EVO_WORKER_HOME", raising=False)
    config = WorkerConfig(url="https://hub.example.org", worker_id=7, name="mac-mini", projects=["demo"])
    WorkerHome().save(config, "evw_" + "s" * 43)
    fake = FakeLaunchctl()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_service_install_prints_the_doctors_high_and_medium_findings_and_installs_all_the_same(
    installing, monkeypatch, capsys
):
    seen = []

    def examine(machine):
        seen.append(machine.path)
        report = Report(user=ME, home=str(machine.home), platform="darwin", path=machine.path)
        report.add("other-admin", HIGH, "m1", "m1 administers this machine (group admin).", "Take m1 out.")
        report.add("runtime-login-stored", MEDIUM, "codex", "Codex is signed in here.", "`codex logout` here.")
        report.add("home-readable", LOW, "~/.evo", "~/.evo is mode 0755.", "chmod")
        return report

    monkeypatch.setattr(doctor, "examine", examine)
    assert main(["worker", "service", "install"]) == 0
    captured = capsys.readouterr()
    assert "daemon: running, pid 4242" in captured.out
    assert seen == [service.service_path()], "the doctor checks the PATH the service keeps"
    assert captured.err.splitlines() == [
        "warning: doctor: high other-admin: m1 administers this machine (group admin).",
        "warning: doctor: medium runtime-login-stored: Codex is signed in here.",
        "warning: doctor: 1 high, 1 medium; `evo-agents worker doctor` says how to fix each. The service is "
        "installed all the same.",
    ]

    def broken(machine):
        raise RuntimeError("no luck")

    monkeypatch.setattr(doctor, "examine", broken)
    assert main(["worker", "service", "install"]) == 0
    captured = capsys.readouterr()
    assert captured.err.splitlines() == ["warning: `evo-agents worker doctor` did not finish (RuntimeError: no luck)"]


def test_low_findings_are_no_warning_of_the_install():
    report = Report(user=ME, home="/home/worker", platform="linux", path="/usr/bin")
    report.add("home-readable", LOW, "~/.evo", "~/.evo is mode 0755.", "chmod")
    assert doctor.warnings(report) == [] and report.exit_status() == 0
    report.skip("ssh-private-key", "ssh-keygen is not on PATH")
    assert doctor.warnings(report) == []


def test_the_doctor_reads_a_real_private_key_as_ssh_keygen_does(home, tools, tmp_path):
    """The fake ssh-keygen stands for the real one: where the real one is installed, a key made without a passphrase
    is found and one made with a passphrase is not."""
    real = subprocess.run(["sh", "-c", "command -v ssh-keygen"], capture_output=True, text=True).stdout.strip()
    if not real:
        pytest.skip("ssh-keygen is not installed")
    ssh = home / ".ssh"
    ssh.mkdir(mode=0o700)
    for name, passphrase in (("id_none", ""), ("id_pass", "a passphrase")):
        made = subprocess.run(
            [real, "-q", "-t", "ed25519", "-N", passphrase, "-C", "doctor", "-f", str(ssh / name)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
        assert made.returncode == 0, made.stderr
    (tools.bin / "ssh-keygen").unlink()
    (tools.bin / "ssh-keygen").symlink_to(real)
    report = doctor.examine(machine_of(home, tools))
    assert sorted(found(report, "ssh-private-key")) == ["~/.ssh/id_none"]
    (ssh / "id_pass").chmod(0o644)
    report = doctor.examine(machine_of(home, tools))
    keys = found(report, "ssh-private-key")
    assert sorted(keys) == ["~/.ssh/id_none", "~/.ssh/id_pass"]
    assert "group or other can read" in keys["~/.ssh/id_pass"].detail
    assert stat.S_IMODE((ssh / "id_none").stat().st_mode) == 0o600
