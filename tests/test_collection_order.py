"""A test under tests/hub keeps the fixtures of tests/hub/conftest.py whatever the order of the paths on pytest's
command line.

``python -m pytest tests/hub/test_a.py tests/test_b.py tests/hub/test_c.py`` collects the package ``tests`` a second
time for the file right under it. pytest 9.1 then made a second ``tests/hub`` node, which the conftest of tests/hub was
not bound to: every test of tests/hub/test_c.py failed with "fixture 'hub_db' not found" and ran without the autouse
fixtures of that conftest (pytest #14635, fixed in 9.2, the floor of the test extra). Plan verify commands mix such
paths, and the worker's batches of at most 10 files (``evo_agents.worker.broad_tests``) mix them in any order."""

import os
import subprocess
import sys
import textwrap

import pytest

TREE = {
    "pytest.ini": "[pytest]\n",
    "tests/__init__.py": "",
    "tests/conftest.py": "",
    "tests/test_top.py": "def test_top():\n    pass\n",
    "tests/hub/__init__.py": "",
    "tests/hub/conftest.py": """
        import pytest


        @pytest.fixture(autouse=True)
        def _isolated(request):
            request.node.isolated = True


        @pytest.fixture
        def hub_db():
            return "db"
    """,
    "tests/hub/test_first.py": """
        def test_first(request, hub_db):
            assert hub_db == "db" and request.node.isolated
    """,
    "tests/hub/test_second.py": """
        def test_second(request, hub_db):
            assert hub_db == "db" and request.node.isolated
    """,
}


@pytest.mark.parametrize(
    "paths",
    [
        ["tests/hub/test_first.py", "tests/test_top.py", "tests/hub/test_second.py"],
        ["tests/test_top.py", "tests/hub/test_first.py", "tests/hub/test_second.py"],
        ["tests/hub/test_first.py", "tests/hub/test_second.py", "tests/test_top.py"],
    ],
    ids=["hub-top-hub", "top-hub", "hub-top"],
)
def test_a_hub_test_finds_its_conftest_fixtures_in_any_order_of_the_paths(tmp_path, paths):
    for name, text in TREE.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(textwrap.dedent(text).lstrip())
    env = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "3 passed" in done.stdout
