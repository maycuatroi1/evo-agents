import json
import os
import subprocess
import sys
import time

import pytest

from evo_agents.cli import WORKER_ON_WINDOWS, main
from evo_agents.hub import hooks
from evo_agents.hub.memory import LeftAlone, read_memory
from evo_agents.kg import search_hint

pytestmark = pytest.mark.skipif(os.name != "nt", reason="what only Windows does")


def run_cli(*args: str, code_page: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    if code_page:
        env["PYTHONIOENCODING"] = code_page
    return subprocess.run([sys.executable, "-m", "evo_agents", *args], capture_output=True, env=env, timeout=120)


def test_the_cli_starts_and_lists_every_group():
    done = run_cli("--help")
    assert done.returncode == 0, done.stderr
    for group in ("harness", "kg", "hub", "worker"):
        assert group in done.stdout.decode("utf-8")


def test_the_worker_says_to_use_wsl(capsys):
    assert main(["worker", "status"]) == 2
    assert capsys.readouterr().err == f"error: {WORKER_ON_WINDOWS}\n"
    done = run_cli("worker", "doctor")
    assert done.returncode == 2 and b"WSL" in done.stderr


def test_output_is_utf8_whatever_the_code_page(tmp_path):
    root = tmp_path / "tiếng-việt-harness"
    root.mkdir()
    (root / "harness.yaml").write_text("name: tiếng-việt\nrepos: []\n", encoding="utf-8")
    done = run_cli("harness", "validate", "--json", str(root), code_page="cp1252")
    found = json.loads(done.stdout.decode("utf-8"))
    assert str(root / "harness.yaml") in [entry["path"] for entry in found["files"]]


def test_a_symlinked_memory_file_is_never_read(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("this account may not create symbolic links")
    with pytest.raises(LeftAlone, match="a symlink"):
        read_memory(link)


def test_the_budget_stops_the_main_thread_without_sigalrm():
    with pytest.raises(hooks.OutOfTime):
        with hooks.budget(0.2):
            for _ in range(100):
                time.sleep(0.05)


def test_git_bash_paths_name_windows_paths():
    assert search_hint._native("/c/Users/someone/repo") == "C:/Users/someone/repo"
    assert search_hint._native("/d") == "D:/"
    assert search_hint._native("/tmp/x") == "/tmp/x"
    assert search_hint._native("C:\\Users\\someone") == "C:\\Users\\someone"
