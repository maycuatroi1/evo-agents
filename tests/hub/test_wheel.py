"""The wheel carries the migrations: built from a copy of the sources, installed into a venv of its own, its
``evo-agents hub migrate`` brings a database to head with nothing from this checkout on the path."""

import importlib.util
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub.migrate import revisions

ROOT = Path(__file__).parents[2]
MIGRATIONS = ROOT / "evo_agents" / "hub" / "migrations"
REVISIONS = revisions()  # every revision of this checkout, oldest first


def migration_files() -> set[str]:
    """Wheel paths of env.py, the script template, every file under versions/ and the vendored SQL in sql/."""
    versions = [
        path
        for path in (MIGRATIONS / "versions").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith(".")
    ]
    assert len(versions) >= len(REVISIONS)  # at least one file per revision
    files = [MIGRATIONS / "env.py", MIGRATIONS / "script.py.mako", *versions, *(MIGRATIONS / "sql").glob("*")]
    return {path.relative_to(ROOT).as_posix() for path in files}


def run(args, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=300, **kwargs)
    assert result.returncode == 0, f"{args} failed:\n{result.stdout}\n{result.stderr}"
    return result


def build_isolation() -> list[str]:
    """Build offline with the installed backend when it is recent enough, else let pip fetch one."""
    if importlib.util.find_spec("setuptools") is None:
        return []
    from importlib.metadata import version

    return ["--no-build-isolation"] if int(version("setuptools").split(".")[0]) >= 77 else []


def test_the_wheel_ships_the_migrations_and_migrates_from_its_own_venv(hub_db, tmp_path):
    source = tmp_path / "source"  # a copy, so the build leaves no build/ or egg-info in the checkout
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "evo_agents", source / "evo_agents", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    dist = tmp_path / "dist"
    pip = [sys.executable, "-m", "pip"]
    run([*pip, "wheel", "--no-deps", *build_isolation(), "--wheel-dir", str(dist), str(source)])
    (wheel,) = dist.glob("evo_ak-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    assert migration_files() <= names

    venv = tmp_path / "venv"
    run([sys.executable, "-m", "venv", "--without-pip", str(venv)])
    bin_dir = venv / ("Scripts" if sys.platform == "win32" else "bin")
    python = bin_dir / ("python.exe" if sys.platform == "win32" else "python")
    run([*pip, "--python", str(python), "install", "--no-deps", "--no-index", str(wheel)])
    # The dependencies come from this interpreter's site-packages, listed after the venv's own: a plain path
    # in a .pth file, so the .pth files there (the editable install of this checkout) are not processed.
    site = Path(run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"]).stdout.strip())
    deps = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
    (site / "outer-site-packages.pth").write_text("".join(f"{path}\n" for path in sorted(deps)), encoding="utf-8")

    env = pg.clean_env(EVO_HUB_DSN=hub_db.dsn)
    env.pop("PYTHONPATH", None)
    where = run(
        [str(python), "-c", "from evo_agents.hub import migrate; print(migrate.script_location())"],
        env=env,
        cwd=tmp_path,
    )
    assert Path(where.stdout.strip()).is_relative_to(venv)

    migrated = run([str(bin_dir / "evo-agents"), "hub", "migrate"], env=env, cwd=tmp_path)
    applied = [line["applied"] for line in pg.log_lines(migrated.stderr) if line["msg"] == "migrations applied"]
    assert applied == [list(REVISIONS)]
    with pg.admin(hub_db.admin_dsn) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [(REVISIONS[-1],)]
        tables = 15 + len(pg.BLOB_TABLES | pg.QUEUE_TABLES | pg.KG_TABLES)
        assert conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'").fetchone()[0] == tables
