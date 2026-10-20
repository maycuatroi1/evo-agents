"""The helper the hub's image runs around pip for its core plugins (evo_agents.plugins.core_install): ``pinned`` and
``extra`` keep the requirement rules of an isolated backend and refuse evo-ak, a package pinned already and one the
image has at another version; ``index`` refuses a wrong URL without showing it; ``check`` fails, naming the entry
point, its distribution, version and reason, when core_plugins() leaves an entry point out or two distributions declare
one name. tests/plugins/test_core_install_image.py builds the image with them."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from importlib.metadata import distributions
from pathlib import Path

import pytest

from evo_agents.plugins import core, core_install
from tests.plugins import core_fakes

ROOT = Path(__file__).resolve().parents[2]
PINNED = ROOT / "deploy/hub/core-plugins.txt"
DIGEST = "ab" * 32
OTHER = "cd" * 32


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = core_install.main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def pinned(tmp_path):
    """Write a pinned file; its path."""

    def write(text: str) -> Path:
        path = tmp_path / "core-plugins.txt"
        path.write_text(text)
        return path

    return write


# pinned


def test_core_install_pinned_file_of_the_repository_is_valid():
    code, out, err = run("pinned", str(PINNED))
    assert code == 0, err
    (line,) = out.splitlines()  # telegram and chats, of the repository evo-agents-core-plugins
    assert re.fullmatch(r"evo-agents-core-plugins==\d+\.\d+\.\d+ --hash=sha256:[0-9a-f]{64}", line), line


def test_core_install_pinned_prints_each_requirement_once_as_pip_reads_it(pinned):
    path = pinned(
        "# the plugins evo-agents ships\n"
        f"lumen-relay==1.0.0 --hash=sha256:{DIGEST} \\\n    --hash=sha256:{OTHER.upper()}\n"
        f"northwind_notices==2.1.0rc1 --hash=sha256:{OTHER}  # a comment\n"
    )
    code, out, err = run("pinned", str(path))
    assert (code, err) == (0, "")
    assert out == (
        f"lumen-relay==1.0.0 --hash=sha256:{DIGEST} --hash=sha256:{OTHER}\n"
        f"northwind_notices==2.1.0rc1 --hash=sha256:{OTHER}\n"
    )


@pytest.mark.parametrize(
    ("line", "says"),
    [
        ("--index-url https://registry.lumen.example/simple", "only from lines `name==version"),
        ("https://registry.lumen.example/lumen_relay-1.0.0-py3-none-any.whl", "only from lines"),
        ("./wheels/lumen_relay-1.0.0-py3-none-any.whl", "only from lines"),
        (f"lumen-relay[extra]==1.0.0 --hash=sha256:{DIGEST}", "only from lines"),
        (f"lumen-relay==1.0.0 ; python_version >= '3.11' --hash=sha256:{DIGEST}", "only from lines"),
        ("lumen-relay==1.0.0", "only from lines"),
        (f"lumen-relay>=1.0 --hash=sha256:{DIGEST}", "only from lines"),
        (f"Evo_AK==0.23.1 --hash=sha256:{DIGEST}", "Evo_AK is the hub itself"),
    ],
)
def test_core_install_pinned_refuses_what_an_isolated_backend_refuses_and_evo_ak(pinned, line, says):
    path = pinned(f"northwind-notices==1.0.0 --hash=sha256:{DIGEST}\n{line}\n")
    code, out, err = run("pinned", str(path))
    assert (code, out) == (1, "")
    assert says in err and str(path) in err
    if "hub itself" not in says:
        assert "line 2" in err


def test_core_install_pinned_refuses_a_package_named_twice_and_a_missing_file(pinned, tmp_path):
    path = pinned(f"lumen-relay==1.0.0 --hash=sha256:{DIGEST}\nLumen_Relay==1.0.1 --hash=sha256:{OTHER}\n")
    code, _, err = run("pinned", str(path))
    assert code == 1 and "Lumen_Relay is named twice" in err
    code, _, err = run("pinned", str(tmp_path / "nowhere.txt"))
    assert code == 1 and "cannot read" in err


# extra


def extra(monkeypatch, value: str, pinned_path: Path) -> tuple[int, str, str]:
    monkeypatch.setenv(core_install.EXTRA_VARIABLE, value)
    return run("extra", str(pinned_path))


def test_core_install_extra_reads_requirements_separated_by_spaces_or_lines(monkeypatch, pinned):
    path = pinned("")
    value = (
        f"lumen-relay==1.0.0 --hash=sha256:{DIGEST} --hash=sha256:{OTHER}"
        f"  lumen-wire==0.3.1 \\\n --hash=sha256:{OTHER}\n\nnorthwind-hooks==2.0.0\t--hash=sha256:{DIGEST}\n"
    )
    code, out, err = extra(monkeypatch, value, path)
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        f"lumen-relay==1.0.0 --hash=sha256:{DIGEST} --hash=sha256:{OTHER}",
        f"lumen-wire==0.3.1 --hash=sha256:{OTHER}",
        f"northwind-hooks==2.0.0 --hash=sha256:{DIGEST}",
    ]
    assert extra(monkeypatch, "  \n", path) == (0, "", "")


@pytest.mark.parametrize(
    ("value", "says"),
    [
        (f"--hash=sha256:{DIGEST} lumen-relay==1.0.0", "requirement 1: a core plugin is installed only from lines"),
        (f"lumen-relay==1.0.0 --hash=sha256:{DIGEST} --index-url https://x.example", "requirement 2"),
        (f"lumen-relay==1.0.0 --hash=sha256:{DIGEST} lumen-wire==1.0", "requirement 2"),
        (f"lumen-relay==1.0.0 --hash=sha256:{DIGEST} #relay", "requirement 2: a core plugin is installed only"),
        (f"lumen-relay==1.0.0 --hash=sha256:{DIGEST} # the relay", "requirement 2"),
        (f"evo-ak==0.23.1 --hash=sha256:{DIGEST}", "evo-ak is the hub itself"),
        (f"lumen-relay==1.0.0 --hash=sha256:{DIGEST} lumen.relay==1.0.1 --hash=sha256:{DIGEST}", "named twice"),
        (f"Northwind.Notices==1.0.0 --hash=sha256:{DIGEST}", "Northwind.Notices is pinned in"),
    ],
)
def test_core_install_extra_refuses_other_forms_evo_ak_and_what_the_pinned_file_names(monkeypatch, pinned, value, says):
    path = pinned(f"northwind-notices==1.0.0 --hash=sha256:{DIGEST}\n")
    code, out, err = extra(monkeypatch, value, path)
    assert (code, out) == (1, "")
    assert says in err and err.startswith("core_install extra: EVO_HUB_EXTRA_CORE_PLUGINS")


def test_core_install_extra_leaves_out_what_the_image_has_and_refuses_another_version_of_it(monkeypatch, pinned):
    path = pinned("")
    installed = core_install.installed_versions()
    assert installed["pyyaml"], "the core package's one dependency"
    have = installed["pyyaml"]
    code, out, err = extra(
        monkeypatch, f"PyYAML=={have} --hash=sha256:{DIGEST} lumen-relay==1.0.0 --hash=sha256:{DIGEST}", path
    )
    assert code == 0 and out == f"lumen-relay==1.0.0 --hash=sha256:{DIGEST}\n"
    assert f"PyYAML {have} is in the image already" in err
    code, out, err = extra(monkeypatch, f"pyyaml==0.1.0 --hash=sha256:{DIGEST}", path)
    assert (code, out) == (1, "")
    assert f"pyyaml==0.1.0, but the image has pyyaml {have}" in err and "may not change a library of the hub" in err


@pytest.mark.skipif(sys.platform == "win32", reason="the value is over the 32767 characters Windows allows a variable")
def test_core_install_extra_takes_at_most_the_requirements_a_backend_takes(monkeypatch, pinned):
    value = " ".join(f"p{n}==1.0 --hash=sha256:{DIGEST}" for n in range(core_install.MAX_REQUIREMENTS + 1))
    code, _, err = extra(monkeypatch, value, pinned(""))
    assert code == 1 and f"over the {core_install.MAX_REQUIREMENTS}" in err


# index

CREDENTIAL = "lumen-deploy-token-0123456789"


def test_core_install_index_takes_https_urls_and_http_to_localhost_or_none(tmp_path):
    code, out, err = run("index", str(tmp_path / "no-secret"))
    assert (code, out) == (0, "") and "from PyPI" in err
    secret = tmp_path / "secret"
    secret.write_bytes(  # bytes: write_text would turn each line break into CRLF on Windows, which the helper refuses
        (
            f"https://deploy:{CREDENTIAL}@registry.lumen.example/pypi/simple\thttps://pypi.org/simple\n"
            f"http://ci:{CREDENTIAL}@127.0.0.1:8765/simple http://localhost/simple http://[::1]:9000/simple\n"
        ).encode()
    )
    code, out, err = run("index", str(secret))
    assert (code, out) == (0, "") and "the 5 URL(s) of the build secret core_plugin_index" in err
    assert CREDENTIAL not in err
    secret.write_bytes(b"\n")
    assert run("index", str(secret))[0] == 0


@pytest.mark.parametrize(
    "url",
    [
        f"http://deploy:{CREDENTIAL}@registry.lumen.example/simple",
        f"ftp://deploy:{CREDENTIAL}@registry.lumen.example/simple",
        f"https://deploy:{CREDENTIAL}@/simple",
        f"https://deploy:{CREDENTIAL}@registry.lumen.example:port/simple",
        f"https://deploy:{CREDENTIAL}@registry.lumen.example/simple#frag",
        f"https://deploy:{CREDENTIAL}@registry.lumen.example/simple\r",
        f"https://deploy:{CREDENTIAL}@registry.lumen.éxample/simple",
        f"registry.lumen.example/{CREDENTIAL}",
    ],
)
def test_core_install_index_refuses_a_wrong_url_without_showing_it(tmp_path, url):
    secret = tmp_path / "secret"
    secret.write_bytes(f"https://pypi.org/simple {url}\n".encode())  # UTF-8 and LF whatever the platform's defaults
    code, out, err = run("index", str(secret))
    assert (code, out) == (1, "")
    assert "URL 2 of 2 in the build secret core_plugin_index (EVO_HUB_CORE_PLUGIN_INDEX_URL)" in err
    assert CREDENTIAL not in err and "lumen" not in err


def test_core_install_index_refuses_a_secret_that_is_not_text_without_showing_it(tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"https://deploy:" + CREDENTIAL.encode() + b"\xff@registry.lumen.example/simple")
    code, _, err = run("index", str(secret))
    assert code == 1 and "cannot read the build secret core_plugin_index: UnicodeDecodeError" in err
    assert CREDENTIAL not in err


# check


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Install fake distributions in a directory of their own, the only ones core_plugins() then sees; the names of
    their modules are unique to the test, so no module of another test stands in for them."""
    site = tmp_path / "site"
    site.mkdir()
    suffix = os.urandom(4).hex()
    monkeypatch.syspath_prepend(str(site))

    def entry_points(*, group: str):
        return [
            point for dist in distributions(path=[str(site)]) for point in dist.entry_points if point.group == group
        ]

    monkeypatch.setattr(core, "entry_points", entry_points)

    def install(name: str, template: str, **options) -> str:
        module = f"{name.replace('-', '_')}_{suffix}"
        core_fakes.write_dist(site, name, template, module=module, **options)
        return module

    yield install
    for name in [name for name in sys.modules if name.endswith(f"_{suffix}")]:
        del sys.modules[name]


def test_core_install_check_lists_the_core_plugins_that_load(installed):
    assert run("check") == (0, "no core plugin is installed\n", "")
    installed("lumen-relay", core_fakes.GOOD, version="1.2.0")
    installed("northwind-hooks", core_fakes.GOOD)
    code, out, err = run("check")
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "core plugin lumen-relay 1.2.0: entry point lumen-relay of lumen-relay 1.2.0",
        "core plugin northwind-hooks 1.0.0: entry point northwind-hooks of northwind-hooks 1.0.0",
        "2 core plugin(s) load",
    ]


def test_core_install_check_fails_on_a_plugin_that_requires_another_api_naming_it(installed):
    # The module the plugin imports needs the hub-server extra, which macOS and Windows CI do not install.
    pytest.importorskip("fastapi")
    installed("lumen-relay", core_fakes.GOOD)
    module = installed("lumen-bridge", core_fakes.WRONG_API, version="2.0.1")
    code, out, err = run("check")
    assert code == 1
    assert "core plugin lumen-relay 1.0.0" in out
    [line, summary] = err.splitlines()
    assert line.startswith(f"entry point lumen-bridge ({module}:manifest) of lumen-bridge 2.0.1 does not load: ")
    # Before the core plugin API (feat/core-plugin-api) the import fails, as on a hub older than the API; with it,
    # require() raises UnsupportedApi naming both versions.
    reason = line.partition(" does not load: ")[2]
    assert "core_plugin_api" in reason or "UnsupportedApi" in reason, reason
    assert summary.startswith("1 problem(s) with the core plugins of this image")


def test_core_install_check_names_each_plugin_core_plugins_leaves_out_and_why(installed):
    installed("lumen-isolated", core_fakes.ISOLATED)
    installed("lumen-future", core_fakes.TOO_NEW, version="3.0.0")
    installed("lumen-list", core_fakes.NOT_A_MANIFEST)
    installed("lumen-broken", core_fakes.BROKEN_IMPORT, version="0.0.1")
    code, out, err = run("check")
    assert (code, out) == (1, "")
    lines = err.splitlines()
    reasons = {line.split(" ", 3)[2]: line for line in lines[:-1]}
    assert sorted(reasons) == ["lumen-broken", "lumen-future", "lumen-isolated", "lumen-list"]
    assert "of lumen-isolated 1.0.0 does not load: ManifestError" in reasons["lumen-isolated"]
    assert "is not a core plugin (tier isolated)" in reasons["lumen-isolated"]
    assert "of lumen-future 3.0.0 does not load:" in reasons["lumen-future"]
    assert "it runs on evo-agents >=99" in reasons["lumen-future"]
    assert "gives list, not a manifest" in reasons["lumen-list"]
    assert "of lumen-broken 0.0.1 does not load: ModuleNotFoundError" in reasons["lumen-broken"]
    assert lines[-1].startswith("4 problem(s)")


def test_core_install_check_fails_when_two_distributions_declare_one_entry_point(installed):
    first = installed("lumen-relay", core_fakes.GOOD, entry_point="relay")
    second = installed("northwind-relay", core_fakes.GOOD, entry_point="relay", version="1.1.0")
    code, out, err = run("check")
    assert code == 1
    assert out.count("entry point relay of") == 2, "both load: the hub would keep the last one's contributions"
    assert (
        "entry point relay is declared by 2 distributions: "
        f"lumen-relay 1.0.0 ({first}:MANIFEST); northwind-relay 1.1.0 ({second}:MANIFEST)"
    ) in err


def test_core_install_check_keeps_the_root_logger_quiet_and_restores_the_core_logger(installed, caplog):
    installed("lumen-broken", core_fakes.BROKEN_IMPORT)
    logger = core.log
    before = (logger.level, logger.propagate, list(logger.handlers))
    with caplog.at_level("WARNING"):
        assert run("check")[0] == 1
    assert [record for record in caplog.records if record.name == core.__name__] == []
    assert (logger.level, logger.propagate, list(logger.handlers)) == before


def test_core_install_check_in_a_process_of_its_own_exits_1_on_a_plugin_that_does_not_load(tmp_path):
    """The build runs ``python -I -m ...``, which reads no PYTHONPATH; here the fake distributions come in through
    PYTHONPATH, so the command runs without -I, with the entry points of this Python and the fake's."""
    site = tmp_path / "site"
    site.mkdir()
    core_fakes.write_dist(site, "lumen-bridge", core_fakes.WRONG_API, module="lumen_bridge_process")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(site), str(ROOT)])}
    done = subprocess.run(
        [sys.executable, "-m", "evo_agents.plugins.core_install", "check"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "entry point lumen-bridge (lumen_bridge_process:manifest) of lumen-bridge 1.0.0 does not load:" in (
        done.stderr
    )
    core_fakes.write_dist(site, "lumen-relay", core_fakes.GOOD, module="lumen_relay_process")
    (site / "lumen_bridge-1.0.0.dist-info" / "entry_points.txt").unlink()
    done = subprocess.run(
        [sys.executable, "-m", "evo_agents.plugins.core_install", "check"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "core plugin lumen-relay 1.0.0: entry point lumen-relay of lumen-relay 1.0.0" in done.stdout
