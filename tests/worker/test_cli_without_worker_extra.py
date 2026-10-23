"""The CLI loads without the worker extra (aiohttp): the hub's image installs ``.[hub-server,graphify]`` and checks
``evo-agents --version``, and a member's core install runs ``evo-agents worker --help``. A module the CLI imports at
load time may import aiohttp only inside the function that needs it."""

from __future__ import annotations

import subprocess
import sys

import pytest

WITHOUT_AIOHTTP = "import sys; sys.modules['aiohttp'] = None; sys.argv = ['evo-agents', *sys.argv[1:]]; " + (
    "from evo_agents.cli import main; main()"
)


@pytest.mark.parametrize("args", [["--version"], ["worker", "--help"], ["worker", "browser", "--help"]])
def test_the_cli_runs_without_aiohttp(args):
    done = subprocess.run([sys.executable, "-c", WITHOUT_AIOHTTP, *args], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
