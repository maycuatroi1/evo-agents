"""``python -m evo_agents.hub.server.core_plugin_api surface`` prints the surface of the API of core plugins as JSON,
as ``tests/plugins/golden/core-plugin-api.json`` holds it; ``... version`` prints the version this checkout serves,
``major.minor``."""

from __future__ import annotations

import json
import sys

from evo_agents.hub.server.core_plugin_api import API_VERSION
from evo_agents.hub.server.core_plugin_api._surface import surface

USAGE = "usage: python -m evo_agents.hub.server.core_plugin_api {surface,version}"


def main(argv: list[str]) -> int:
    if argv == ["surface"]:
        sys.stdout.write(json.dumps(surface(), indent=2, ensure_ascii=False) + "\n")
        return 0
    if argv == ["version"]:
        sys.stdout.write(".".join(map(str, API_VERSION)) + "\n")
        return 0
    sys.stderr.write(USAGE + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
