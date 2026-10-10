"""The production compose file passes the hub every variable its configuration reads.

A container gets only the variables its compose file names, so a variable ``evo_agents.hub.config`` reads that the
block x-hub-environment of deploy/hub/docker-compose.yml leaves out keeps its default in production, whatever the
platform's environment says. deploy/hub/.env.example is where an operator finds them all.

The variables the configuration reads are found two ways: the ``EVO_HUB_*`` names written in config.py, and the names
its ``load_*`` functions look up in an environment that holds only a DSN, which also catches a name built from a
prefix. NOT_PASSED lists those left out on purpose, each with its reason. A failure names the lines to add.

Standard library and PyYAML only: this runs without Postgres and without the server stack.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from evo_agents.hub import config

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/hub/docker-compose.yml"
ENV_EXAMPLE = ROOT / "deploy/hub/.env.example"
CONFIG = Path(config.__file__)
BLOCK = "x-hub-environment"
NAME = re.compile(r"EVO_HUB_[A-Z0-9_]*[A-Z0-9]")  # a prefix such as EVO_HUB_S3_ ends with _ and is no variable
DSN = "postgresql://hub@db/hub"

# Read by the configuration and left out of compose on purpose.
NOT_PASSED = {
    "EVO_HUB_HOST": "the image's command passes --host 0.0.0.0 to hub serve, which wins over the variable",
    "EVO_HUB_PORT": "the image's command passes --port 8080 to hub serve; the compose file and the proxy expect 8080",
    "EVO_HUB_DATA_DIR": "the image sets it to /cache, the volume of api and worker",
    "EVO_HUB_GITHUB_URL": "tests and Playwright point the hub at a fake GitHub; production uses the default",
    "EVO_HUB_GITHUB_API_URL": "tests and Playwright point the hub at a fake GitHub; production uses the default",
    "EVO_HUB_GITHUB_TIMEOUT": "tests shorten it for the fake GitHub; production uses the default",
    "EVO_HUB_TELEGRAM_API_URL": "tests point the hub at a fake Bot API; production uses the default",
}


class Recording(dict):
    """An environment that remembers every name looked up in it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read: set[str] = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read.add(key)
        return super().__contains__(key)


def loaders() -> list:
    return [value for name, value in vars(config).items() if name.startswith("load_") and callable(value)]


def written_in_config() -> set[str]:
    tree = ast.parse(CONFIG.read_text(), filename=str(CONFIG))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and NAME.fullmatch(node.value)
    }


def looked_up_by_config() -> set[str]:
    env = Recording(EVO_HUB_DSN=DSN)
    for load in loaders():
        load(env)
    return {name for name in env.read if NAME.fullmatch(name)}


def read_by_config() -> set[str]:
    return written_in_config() | looked_up_by_config()


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def passed_by_compose() -> dict[str, str]:
    return compose()[BLOCK]


def default_of(name: str, value: str) -> str | None:
    """The default of a line ``NAME: ${NAME:-default}`` of the block, empty when there is none; None for another
    shape."""
    found = re.fullmatch(rf"\$\{{{name}:-([^}}]*)\}}", value)
    return None if found is None else found[1]


def listed_in_env_example() -> set[str]:
    return set(re.findall(r"^(EVO_HUB_[A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), flags=re.MULTILINE))


def test_compose_passes_every_variable_the_hub_config_reads():
    read = read_by_config()
    assert {"EVO_HUB_DSN", "EVO_HUB_S3_BUCKET", "EVO_HUB_GITHUB_APP_ID", "EVO_HUB_LOG_LEVEL"} <= read, "the scan broke"
    wanted = sorted(read - NOT_PASSED.keys())
    in_compose, in_example = passed_by_compose(), listed_in_env_example()
    missing_compose = [name for name in wanted if name not in in_compose]
    missing_example = [name for name in wanted if name not in in_example]
    message = []
    if missing_compose:
        lines = "\n".join(f"  {name}: ${{{name}:-}}" for name in missing_compose)
        message.append(f"add under {BLOCK} in {COMPOSE.relative_to(ROOT)}:\n{lines}")
    if missing_example:
        lines = "\n".join(f"{name}=" for name in missing_example)
        message.append(f"add to {ENV_EXAMPLE.relative_to(ROOT)}, with a comment saying what it does:\n{lines}")
    assert not message, (
        f"{CONFIG.relative_to(ROOT)} reads variables production would never get, so they would keep their default:\n"
        + "\n".join(message)
        + "\nand describe them in the variable table of docs/hub.md. A variable only tests change, or one the image "
        "sets, goes in NOT_PASSED of tests/hub/test_compose.py instead, with the reason."
    )


def test_compose_passes_only_what_the_hub_config_reads_and_not_passed_stays_true():
    read, in_compose = read_by_config(), passed_by_compose()
    stale = sorted(set(in_compose) - read)
    assert not stale, (
        f"{COMPOSE.relative_to(ROOT)} passes {', '.join(stale)}, which {CONFIG.relative_to(ROOT)} no longer reads: "
        f"remove the line from {BLOCK} and from {ENV_EXAMPLE.relative_to(ROOT)}"
    )
    gone = sorted(NOT_PASSED.keys() - read)
    assert not gone, f"{CONFIG.relative_to(ROOT)} no longer reads {', '.join(gone)}: remove it from NOT_PASSED"
    passed_anyway = sorted(NOT_PASSED.keys() & set(in_compose))
    assert not passed_anyway, (
        f"{COMPOSE.relative_to(ROOT)} passes {', '.join(passed_anyway)}, which NOT_PASSED says it leaves out: "
        "remove the line, or remove the entry from NOT_PASSED"
    )


def test_compose_gives_api_and_worker_the_block_each_line_as_name_colon_dash_default():
    services = compose()["services"]
    block = passed_by_compose()
    for service in ("api", "worker"):
        assert services[service]["environment"] == block, f"{service} must take environment: *hub-environment"
    malformed = [f"  {name}: {value}" for name, value in block.items() if default_of(name, value) is None]
    assert not malformed, (
        f"each line of {BLOCK} is NAME: ${{NAME:-default}}, the default empty unless it equals the hub's own, so "
        "`docker compose ps` works without the environment:\n" + "\n".join(malformed)
    )


def test_compose_defaults_are_the_hub_config_defaults():
    base = {"EVO_HUB_DSN": DSN}
    differ = []
    for name, value in passed_by_compose().items():
        default = default_of(name, value)
        if not default or name == "EVO_HUB_DSN":
            continue
        for load in loaders():
            if load(dict(base)) != load({**base, name: default}):
                differ.append(f"  {name}: {value}")
                break
    assert not differ, (
        f"these defaults of {COMPOSE.relative_to(ROOT)} differ from those of {CONFIG.relative_to(ROOT)}, so production "
        "runs with another value than the code and docs/hub.md say; make the compose default empty, or equal to "
        "the hub's:\n" + "\n".join(differ)
    )
