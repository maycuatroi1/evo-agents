"""Import every module of evo_agents and exit 1 naming the ones that fail.

The ci job `py311` runs it on Python 3.11, the oldest version requires-python names, with every extra installed: a
name of the standard library added after 3.11 or a syntax only a later Python reads fails here at import time.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

import evo_agents

SKIP = (".migrations.versions.", ".plugin_migrations.")  # alembic revisions load through alembic, not an import
MAIN = "__main__"  # runs the command line when imported


def main() -> int:
    failed = []
    count = 0
    for module in pkgutil.walk_packages(evo_agents.__path__, "evo_agents."):
        if any(part in module.name for part in SKIP) or module.name.rsplit(".", 1)[-1] == MAIN:
            continue
        count += 1
        try:
            importlib.import_module(module.name)
        except Exception as error:  # noqa: BLE001 - every failure is reported
            failed.append(f"{module.name}: {type(error).__name__}: {error}")
    for line in failed:
        print(line, file=sys.stderr)
    print(f"{count - len(failed)} of {count} modules imported on Python {sys.version.split()[0]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
