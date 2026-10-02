"""Builders for small but complete projects: a harness plus a git repo, with knowledge.yaml."""

import os
import subprocess
from pathlib import Path

from evo_agents.kg.project import load_project_at

DATE = "2026-10-01T09:00:00+07:00"


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": DATE,
        "GIT_COMMITTER_DATE": DATE,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str = "change") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


GUIDE = """# Guide

Intro to the app. See `app/search.py::search` and KB-01.

## Setup

Run the server from app/server.py. Seam `search-api` is owned by app.

### Details

Uses `rank_results` internally.

## Usage

Call KB-02 from the plan demo#2.
"""


def make_project(
    base: Path, home: Path, name: str = "demo", level: str = "internal", app_level: str = "internal"
) -> object:
    """Create <base>/<name>-harness and <base>/<name>-app; return the loaded Project."""
    app = base / f"{name}-app"
    write(app / "app/__init__.py", "")
    write(
        app / "app/search.py",
        "def rank_results(items):\n    return sorted(items)\n\n\n"
        "def search(query):\n    return rank_results([query])\n",
    )
    write(
        app / "app/server.py",
        "from app.search import search\n\n\nclass Server:\n    def handle(self, q):\n        return search(q)\n",
    )
    write(app / "docs/guide.md", GUIDE)
    write(app / "docs/old.md", "# Old\n\nObsolete notes about app/server.py.\n")
    git(app, "init", "-q", "-b", "main")
    git(app, "remote", "add", "origin", f"git@github.com:acme/{name}-app.git")
    commit(app, "init")

    root = base / f"{name}-harness"
    write(
        root / "harness.yaml",
        f"""name: {name}
workspace: ..
knowledge_file: knowledge.yaml
repos:
  - name: app
    path: {name}-app
    origin: git@github.com:acme/{name}-app.git
    default_branch: main
""",
    )
    write(
        root / "contracts.yaml",
        """seams:
  - name: search-api
    owner: app
    consumers: [web]
    source: {repo: app, path: app/search.py}
    verify: pytest
""",
    )
    write(
        root / "plans/active/demo.yaml",
        """id: demo
goal: Ship search for KB-01.
repos:
  - repo: app
steps:
  - id: 1
    title: Ranking
    repo: app
    what: Implement rank_results in app/search.py.
    status: done
    evidence: app@1a2b3c4d
  - id: 2
    title: Server
    repo: app
    what: Wire app/server.py to search.
    depends_on: [1]
    status: pending
seams_touched:
  - name: search-api
""",
    )
    write(
        root / "bindings/core.yaml",
        """version: 1
bindings:
  - from: app:app/search.py::search
    rel: implements
    to: usecase:KB-01
""",
    )
    write(root / "CLUSTER.md", "# Demo\n\nRepos: app. Plan demo. Guide in docs/guide.md.\n")
    write(
        root / "knowledge.yaml",
        f"""version: 1
project: {name}
policy:
  levels: [public, internal, customer]
  sinks:
    - id: agent
      kind: agent-session
      clearance: {{level: internal}}
    - id: wide
      kind: agent-session
      clearance: {{level: customer}}
sources:
  - id: harness
    connector: harness
    label: {{level: {level}, integrity: U}}
  - id: app
    connector: git
    repo: app
    label: {{level: {app_level}, integrity: U}}
identifiers:
  - kind: UseCase
    pattern: '\\bKB-\\d{{2}}\\b'
""",
    )
    return load_project_at(root, home)
