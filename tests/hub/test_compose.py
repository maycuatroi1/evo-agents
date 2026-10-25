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
from evo_agents.hub.model_catalog import GATEWAY_VERSION
from evo_agents.plugins.environment import NPROC

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/hub/docker-compose.yml"
ENV_EXAMPLE = ROOT / "deploy/hub/.env.example"
CONFIG = Path(config.__file__)
BLOCK = "x-hub-environment"
GATEWAY_BLOCK = "x-gateway-environment"  # the model gateway's variables, the api's alone
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
    """Every variable compose passes the hub: x-hub-environment, to api and worker, and x-gateway-environment, to the
    api alone."""
    data = compose()
    return {**data[BLOCK], **data[GATEWAY_BLOCK]}


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
    data = compose()
    services, block = data["services"], passed_by_compose()
    assert services["api"]["environment"] == block, "api must take <<: [*hub-environment, *gateway-environment]"
    assert services["worker"]["environment"] == data[BLOCK], "worker must take environment: *hub-environment"
    assert not set(data[GATEWAY_BLOCK]) & set(data[BLOCK]), f"{GATEWAY_BLOCK} is the api's alone"
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


# The model gateway (docs/hub.md, section Model gateway)

GATEWAY_VARIABLES = {"EVO_HUB_GATEWAY_URL", "EVO_HUB_GATEWAY_SECRET"}


def test_gateway_compose_variables_reach_the_api_alone_and_default_to_off():
    data = compose()
    assert set(data[GATEWAY_BLOCK]) == GATEWAY_VARIABLES == set(config.GATEWAY_VARIABLES)
    assert all(default_of(name, value) == "" for name, value in data[GATEWAY_BLOCK].items()), (
        "unset: the gateway is off"
    )
    assert GATEWAY_VARIABLES <= listed_in_env_example()
    for name, service in data["services"].items():
        environment = set(service.get("environment") or {})
        expected = {"api": GATEWAY_VARIABLES, "gateway": {"EVO_HUB_GATEWAY_SECRET"}}.get(name, set())
        assert environment & GATEWAY_VARIABLES == expected, f"{name} must not hold the gateway's credential"
    assert config.load_config({"EVO_HUB_DSN": DSN}).gateway_missing() == ["EVO_HUB_GATEWAY_SECRET"]
    on = config.load_config({"EVO_HUB_DSN": DSN, "EVO_HUB_GATEWAY_SECRET": "s" * 32})
    assert on.gateway_url == config.DEFAULT_GATEWAY_URL and not on.gateway_missing()


def test_gateway_compose_runs_the_pinned_image_on_its_own_network_without_a_port():
    data = compose()
    gateway, api = data["services"]["gateway"], data["services"]["api"]
    image, _, digest = gateway["image"].partition("@")
    assert image == f"diegosouzapw/omniroute:{GATEWAY_VERSION}" and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    assert "ports" not in gateway and "labels" not in gateway, "no published port, no domain"
    assert set(gateway["networks"]) == {"gateway"}, "neither the database (platform) nor web (default)"
    assert "evo-agents-hub-gateway" in gateway["networks"]["gateway"]["aliases"]
    assert "gateway" in api["networks"] and data["networks"]["gateway"] in ({}, None)
    on_gateway = sorted(
        name for name, service in data["services"].items() if "gateway" in (service.get("networks") or {})
    )
    assert on_gateway == ["api", "gateway"], "only the api reaches the gateway"
    assert (
        gateway["environment"]["REQUIRE_API_KEY"] == "true" and gateway["environment"]["ENABLE_REQUEST_LOGS"] == "false"
    )
    assert gateway["cap_drop"] == ["ALL"] and "no-new-privileges:true" in gateway["security_opt"]
    assert {"cpus", "mem_limit"} <= set(gateway) and "env_file" not in gateway and "secrets" not in gateway
    script = gateway["entrypoint"][-1]
    assert "exit 0" in script.split("\n")[0] and "unset derived EVO_HUB_GATEWAY_SECRET" in script


# The plugins service, which runs third-party code

PLUGINS_ENVIRONMENT = {"EVO_HUB_PLUGIN_HOST_TOKEN", "EVO_HUB_LOG_LEVEL"}  # all the plugin host reads
PLUGINS_CAPABILITIES = ["SETUID", "SETGID", "CHOWN", "KILL"]  # a user of its own for each backend, and no more


def test_compose_gives_the_plugin_isolation_service_no_hub_variable():
    plugins = compose()["services"]["plugins"]
    environment = plugins["environment"]
    assert isinstance(environment, dict) and environment is not passed_by_compose(), "never *hub-environment"
    assert set(environment) == PLUGINS_ENVIRONMENT, (
        f"the plugins service runs third-party code and takes only {sorted(PLUGINS_ENVIRONMENT)}; "
        f"{sorted(set(environment) - PLUGINS_ENVIRONMENT)} would reach it"
    )
    assert "env_file" not in plugins and "secrets" not in plugins
    assert set(plugins["networks"]) == {"default"}, "not on the platform's network, where the database is"


def test_compose_runs_the_plugin_isolation_service_as_root_with_four_capabilities_and_limits():
    services = compose()["services"]
    plugins = services["plugins"]
    assert plugins["user"] == "0:0" and plugins["cap_drop"] == ["ALL"]
    assert plugins["cap_add"] == PLUGINS_CAPABILITIES
    assert plugins["init"] is True and "no-new-privileges:true" in plugins["security_opt"]
    assert {"cpus", "mem_limit", "pids_limit"} <= set(plugins), "a backend must not starve the hub's machine"
    pids = int(default_of("EVO_HUB_PLUGINS_PIDS", plugins["pids_limit"]))
    assert pids >= (8 + 1) * NPROC, (
        f"the review of PR #72: pids_limit {pids} with {NPROC} processes per plugin user lets a few plugins at their "
        "limit take every pid of the service; eight of them must leave as much again to the host"
    )
    mounts = {volume.split(":")[1] for volume in plugins["volumes"]}
    assert mounts == {"/plugins"} and "--data-dir" in plugins["command"] and "/plugins" in plugins["command"]
    others = {volume.split(":")[0] for name in ("api", "worker") for volume in services[name].get("volumes", [])}
    assert not others & {volume.split(":")[0] for volume in plugins["volumes"]}, "the plugins volume is its own"


# The build: a hub's own core plugins beside the pinned ones (docs/hub-deployment.md, section Core plugins in the image)

BUILD = "x-hub-build"
EXTRA = "EVO_HUB_EXTRA_CORE_PLUGINS"  # a build argument: the requirements, no secret
INDEX = "EVO_HUB_CORE_PLUGIN_INDEX_URL"  # the index URL, which may hold a credential: the build secret alone
INDEX_SECRET = "core_plugin_index"
HUB_SERVICES = ("api", "worker", "plugins")  # the services of the hub's image
DOCKERFILE = ROOT / "deploy/hub/Dockerfile"
DOCKERIGNORE = ROOT / "deploy/hub/Dockerfile.dockerignore"
PIP_FLAGS = "--no-deps --require-hashes --only-binary=:all:"


def test_compose_builds_the_hub_services_from_one_definition_with_the_extra_plugins_and_the_index_secret():
    data = compose()
    build = data[BUILD]
    assert build["context"] == "../.." and build["dockerfile"] == "deploy/hub/Dockerfile"
    assert build["args"] == {EXTRA: f"${{{EXTRA}:-}}"}, "unset must build as before: an empty argument"
    assert build["secrets"] == [INDEX_SECRET]
    assert data["secrets"] == {INDEX_SECRET: {"environment": INDEX}}
    for service in HUB_SERVICES:
        assert data["services"][service]["build"] is build, f"{service} must take build: *hub-build"


def test_compose_keeps_the_build_variables_out_of_every_service():
    data = compose()
    assert not {EXTRA, INDEX} & set(passed_by_compose()), f"{BLOCK} passes what config.py reads, and it reads neither"
    for name, service in data["services"].items():
        environment = service.get("environment") or {}
        text = yaml.safe_dump(environment)
        assert EXTRA not in text and INDEX not in text, f"{name} would hold the build's variables at run time"
        assert "secrets" not in service, f"{name}: a service's secret is a file in its running container"
        assert "env_file" not in service
    assert {EXTRA, INDEX} <= listed_in_env_example()


def _instructions(text: str) -> list[str]:
    """The instructions of a Dockerfile, each on one line, comments left out."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return [" ".join(part.split()) for part in "\n".join(lines).replace("\\\n", " ").splitlines() if part.strip()]


def test_dockerfile_installs_the_pinned_then_the_extra_core_plugins_into_the_venv_and_checks_them():
    instructions = _instructions(DOCKERFILE.read_text())
    stages = [index for index, line in enumerate(instructions) if line.startswith("FROM ")]
    assert len(stages) == 2, "a build stage and the runtime stage"
    build, runtime = instructions[stages[0] : stages[1]], instructions[stages[1] :]
    joined = "\n".join(build)
    pinned = joined.index("deploy/hub/core-plugins.txt /tmp/core-plugins.txt")  # copied into the build stage
    hub = joined.index("pip install '.[hub-server,graphify]'")
    extra = joined.index(f'ARG {EXTRA}=""')
    secret = joined.index(f"RUN --mount=type=secret,id={INDEX_SECRET},required=false")
    check = joined.index("python -I -m evo_agents.plugins.core_install check")
    assert hub < pinned < extra < secret < check, "the hub, then the pinned plugins, then the extra ones, then check"
    installs = [line for line in build if "-r /tmp/" in line]
    assert len(installs) == 2 and all(line.count(PIP_FLAGS) == 1 for line in installs), (
        f"pip installs the core plugins with {PIP_FLAGS}: exactly the lines core_install wrote, each against its hash"
    )
    assert "--index-url https://pypi.org/simple -r /tmp/pinned.txt" in installs[0], "the pinned ones from PyPI alone"
    assert "/run/secrets/core_plugin_index" in installs[1] and "pip check" in installs[1]
    later = "\n".join(runtime)
    assert EXTRA not in later and "secret" not in later, "the runtime stage gets neither the argument nor the secret"
    assert [line for line in runtime if line.startswith("COPY")] == ["COPY --from=build /opt/venv /opt/venv"]
    assert "!deploy/hub/core-plugins.txt" in DOCKERIGNORE.read_text().splitlines()
