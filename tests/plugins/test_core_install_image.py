"""The hub's image with a core plugin of a hub's own (docs/hub-deployment.md, section Core plugins in the image), built
as CI's job core-plugins-image builds it: the build argument EVO_HUB_EXTRA_CORE_PLUGINS names a wheel, which comes
from an index that wants a credential, given in the build secret core_plugin_index (the compose file's
EVO_HUB_CORE_PLUGIN_INDEX_URL). The hub of that image loads the plugin: ``core_install check`` lists it, and
``evo-agents hub plugin list`` shows it installed by the image, against the image's api on a Postgres of its own. The
credential is in no layer of the image, nor its configuration, nor ``docker history``, nor the build's log. A plugin
that requires another core plugin API than the hub serves fails the build at ``check``, which names it.

Docker runs everything. The index serves on the Docker host's network, where the build's steps reach it at 127.0.0.1
(``docker build --network host``); Postgres, a fake GitHub that answers GET /user and the api run on a network of
their own, and the CLI runs in the api's container, against 127.0.0.1. It takes a few minutes and Docker, so it runs
only with EVO_TEST_HUB_IMAGE=1; with it set, a missing Docker fails the test.
"""

from __future__ import annotations

import gzip
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from tests.plugins import core_fakes

pytestmark = pytest.mark.skipif(
    os.environ.get("EVO_TEST_HUB_IMAGE") != "1",
    reason="EVO_TEST_HUB_IMAGE=1 builds the hub's image with Docker (CI job core-plugins-image)",
)

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy/hub/Dockerfile"
POSTGRES_IMAGE = "postgres:16.15-trixie"  # the dev stack's (deploy/hub/docker-compose.dev.yml)
PLUGIN, VERSION, MODULE = "lumen-relay", "1.0.0", "lumen_relay"
ADMIN, ADMIN_ID = "ci-admin", 4242
INDEX_USER = "lumen-deploy"
GZIP_MAGIC, ZSTD_MAGIC = b"\x1f\x8b", b"\x28\xb5\x2f\xfd"
CHUNK = 1 << 20

# The index: PEP 503 pages are the directory listings of /srv, each answered only with the credential's Basic header;
# it prints a line for each request, which the test reads back.
INDEX_SERVER = r"""
import base64, functools, http.server, sys
port, user, password = int(sys.argv[1]), sys.argv[2], sys.argv[3]
expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != expected:
            print("refused", self.path, flush=True)
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="lumen"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        print("authorized", self.path, flush=True)
        super().do_GET()

    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory="/srv"))
print("ready", flush=True)
server.serve_forever()
"""

# GitHub as the hub's sign-in asks it: GET /user with the one token it issued.
GITHUB = r"""
import http.server, json, sys
port, login, user_id, token = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/user" and self.headers.get("Authorization") == f"Bearer {token}":
            status, body = 200, json.dumps({"login": login, "id": user_id}).encode()
        else:
            status, body = 401, b'{"message": "Bad credentials"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
"""

# In the api's container: trade the fake GitHub's token for a hub token, sign the CLI in with it, list the plugins.
SIGN_IN_AND_LIST = r"""
token=$(python -c '
import json, os, urllib.request
body = json.dumps({"github_token": os.environ["GITHUB_TOKEN"], "host": "ci"}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8080/v1/auth/github", data=body, headers={"Content-Type": "application/json"}
)
print(json.load(urllib.request.urlopen(request, timeout=30))["token"])
')
umask 077
mkdir -p /tmp/hub
printf %s "$token" > /tmp/hub/token
printf '{"url": "http://127.0.0.1:8080", "login": "%s"}' "$ADMIN" > /tmp/hub/config.json
export EVO_HUB_HOME=/tmp/hub
evo-agents hub plugin list
evo-agents hub plugin show "$PLUGIN"
"""


def docker(
    *args: str, timeout: float = 120, env: dict | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, env=env)
    if check and done.returncode != 0:
        raise AssertionError(
            f"docker {args[0]} exited {done.returncode}:\n{done.stdout[-6000:]}\n{done.stderr[-6000:]}"
        )
    return done


def python_image() -> str:
    """The base image of the hub's Dockerfile, which the build pulls anyway."""
    return re.search(r"^ARG PYTHON_IMAGE=(\S+)$", DOCKERFILE.read_text(), flags=re.MULTILINE)[1]


def wait(what: str, ready, seconds: float, logs=None) -> None:
    deadline = time.monotonic() + seconds
    while not ready():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} not ready after {seconds:.0f} s" + (f":\n{logs()}" if logs else ""))
        time.sleep(1)


def logs(container: str) -> str:
    done = docker("logs", container, check=False)
    return (done.stdout + done.stderr)[-6000:]


def running(container: str) -> bool:
    done = docker("inspect", "-f", "{{.State.Running}}", container, check=False)
    return done.stdout.strip() == "true"


def serve_index(directory: Path, name: str, password: str) -> int:
    """Start the index on the Docker host's network; the port it listens on."""
    for _ in range(5):
        port = 20000 + secrets.randbelow(40000)
        docker("rm", "-f", name, check=False)
        docker(
            "run", "-d", "--name", name, "--network", "host", "-v", f"{directory}:/srv:ro",
            python_image(), "python", "-c", INDEX_SERVER, str(port), INDEX_USER, password,
        )  # fmt: skip
        wait("the index", lambda: "ready" in logs(name) or not running(name), 60)
        if running(name):
            return port
    raise AssertionError(f"the index did not start:\n{logs(name)}")


def search_stream(stream, needles: list[bytes], found: set[bytes]) -> None:
    """Add to ``found`` each of ``needles`` the bytes of ``stream`` hold, read a chunk at a time, through gzip when
    the stream is gzip; a zstd stream fails the test, which could not read it."""
    head = stream.read(4)
    if head.startswith(ZSTD_MAGIC):
        raise AssertionError("a layer of `docker save` is zstd, which this test cannot read")
    if head.startswith(GZIP_MAGIC):
        stream = gzip.GzipFile(fileobj=_Prefixed(head, stream))
        head = b""
    overlap = max(len(needle) for needle in needles)
    tail = head
    while True:
        chunk = stream.read(CHUNK)
        window = tail + chunk
        found.update(needle for needle in needles if needle in window)
        if not chunk:
            return
        tail = window[-overlap:]


class _Prefixed:
    """A file object that answers ``prefix`` first, then the rest of ``stream``."""

    def __init__(self, prefix: bytes, stream):
        self.prefix, self.stream = prefix, stream

    def read(self, size: int = -1) -> bytes:
        if self.prefix:
            out, self.prefix = self.prefix, b""
            return out + (self.stream.read(size - len(out)) if size > len(out) else b"")
        return self.stream.read(size)


def saved_image_holds(image: str, needles: list[bytes]) -> set[bytes]:
    """Which of ``needles`` the archive of ``docker save`` holds: the image's configuration with its history, and every
    file of every layer."""
    found: set[bytes] = set()
    with subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE) as proc:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for member in archive:
                if member.isfile():
                    search_stream(archive.extractfile(member), needles, found)
    assert proc.returncode == 0, "docker save failed"
    return found


@pytest.fixture
def names():
    """Names of this test's containers, network and image, all removed after it."""
    suffix = secrets.token_hex(4)
    made = {
        "image": f"evo-agents-hub:core-plugins-image-{suffix}",
        "network": f"evo-core-plugins-{suffix}",
        **{part: f"evo-core-plugins-{part}-{suffix}" for part in ("index", "postgres", "github", "api")},
    }
    yield made
    for part in ("index", "postgres", "github", "api"):
        docker("rm", "-f", made[part], check=False)
    docker("network", "rm", made["network"], check=False)
    docker("image", "rm", "-f", made["image"], check=False)


def test_core_install_image_installs_a_plugin_from_a_private_index_and_keeps_no_credential(tmp_path, names):
    assert shutil.which("docker"), "EVO_TEST_HUB_IMAGE=1 needs Docker"
    password = "evo-fake-index-credential-" + secrets.token_hex(12)
    marker = "lumen-relay-layer-marker-" + secrets.token_hex(12)
    index = tmp_path / "index"
    wheel, digest = core_fakes.plugin_wheel(index / "simple" / PLUGIN, PLUGIN, VERSION, MODULE, marker)
    (index / "mirror" / PLUGIN).mkdir(parents=True)
    shutil.copy(wheel, index / "mirror" / PLUGIN / wheel.name)
    port = serve_index(index, names["index"], password)

    # The build: the argument names the wheel, the secret two indexes of the same server, both behind the credential.
    base = f"http://{INDEX_USER}:{password}@127.0.0.1:{port}"
    env = {**os.environ, "EVO_HUB_CORE_PLUGIN_INDEX_URL": f"{base}/simple\n{base}/mirror"}
    built = docker(
        "build", "--progress=plain", "--network", "host", "-f", str(DOCKERFILE),
        "--build-arg", f"EVO_HUB_EXTRA_CORE_PLUGINS={PLUGIN}=={VERSION} --hash=sha256:{digest}",
        "--secret", "id=core_plugin_index,env=EVO_HUB_CORE_PLUGIN_INDEX_URL",
        "-t", names["image"], str(ROOT),
        timeout=1800, env=env,
    )  # fmt: skip
    build_log = built.stdout + built.stderr
    requests = logs(names["index"]).splitlines()
    pages = {line for line in requests if line.startswith("authorized ") and line.endswith(f"/{PLUGIN}/")}
    fetched = [line for line in requests if line.startswith("authorized ") and line.endswith(f"/{wheel.name}")]
    assert pages == {f"authorized /simple/{PLUGIN}/", f"authorized /mirror/{PLUGIN}/"} and fetched, (
        "pip read both indexes of the secret and fetched the wheel with its credential:\n" + "\n".join(requests)
    )
    assert f"core plugin {PLUGIN} {VERSION}: entry point {PLUGIN} of {PLUGIN} {VERSION}" in build_log
    assert password not in build_log, "pip printed the index's credential in the build's log"

    # No credential in the image: its history, its configuration, and every file of every layer.
    history = docker("history", "--no-trunc", "--format", "{{.CreatedBy}}", names["image"]).stdout
    config = docker("image", "inspect", names["image"]).stdout
    assert password not in history and password not in config
    found = saved_image_holds(names["image"], [password.encode(), marker.encode()])
    assert marker.encode() in found, "the search did not reach the plugin's file in the layers"
    assert password.encode() not in found, "a layer of the image holds the index's credential"

    # The hub of the image loads it.
    checked = docker("run", "--rm", names["image"], "python", "-I", "-m", "evo_agents.plugins.core_install", "check")
    assert f"core plugin {PLUGIN} {VERSION}: entry point {PLUGIN} of {PLUGIN} {VERSION}" in checked.stdout

    docker("network", "create", names["network"])
    docker(
        "run", "-d", "--name", names["postgres"], "--network", names["network"], "--network-alias", "db",
        "-e", "POSTGRES_PASSWORD=ci-postgres-password", POSTGRES_IMAGE,
    )  # fmt: skip
    github_token = "gho_" + secrets.token_hex(18)
    docker(
        "run", "-d", "--name", names["github"], "--network", names["network"], "--network-alias", "github",
        python_image(), "python", "-c", GITHUB, "8000", ADMIN, str(ADMIN_ID), github_token,
    )  # fmt: skip
    wait(
        "Postgres",
        lambda: (
            docker("exec", names["postgres"], "pg_isready", "-U", "postgres", "-h", "127.0.0.1", check=False).returncode
            == 0
        ),
        90,
    )
    docker(
        "run", "-d", "--name", names["api"], "--network", names["network"],
        "-e", "EVO_HUB_DSN=postgresql://postgres:ci-postgres-password@db:5432/postgres",
        "-e", f"EVO_HUB_ADMINS={ADMIN}", "-e", "EVO_HUB_GITHUB_CLIENT_ID=Iv1.ci0core0plugins",
        "-e", "EVO_HUB_GITHUB_API_URL=http://github:8000", names["image"],
    )  # fmt: skip
    live = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/v1/health/live', timeout=4)"
    wait(
        "the api",
        lambda: (
            not running(names["api"]) or docker("exec", names["api"], "python", "-c", live, check=False).returncode == 0
        ),
        240,
        lambda: logs(names["api"]),
    )
    assert running(names["api"]), f"the api stopped:\n{logs(names['api'])}"
    listed = docker(
        "exec", "-e", f"GITHUB_TOKEN={github_token}", "-e", f"ADMIN={ADMIN}", "-e", f"PLUGIN={PLUGIN}",
        names["api"], "sh", "-ec", SIGN_IN_AND_LIST, check=False,
    )  # fmt: skip
    assert listed.returncode == 0, f"{listed.stdout}\n{listed.stderr}\n{logs(names['api'])}"
    [row] = [line.split() for line in listed.stdout.splitlines() if line.startswith(f"{PLUGIN} ")]
    assert VERSION in row and "lumen" in row, listed.stdout
    assert f"installed {VERSION} by the hub's image" in listed.stdout


def test_core_install_image_build_fails_on_a_plugin_that_requires_another_api(tmp_path, names):
    """The build of a hub whose extra plugin would not load fails at check, naming the plugin; the layers before
    are those of the test above, so it costs little more than the step that fails."""
    assert shutil.which("docker"), "EVO_TEST_HUB_IMAGE=1 needs Docker"
    password = "evo-fake-index-credential-" + secrets.token_hex(12)
    index = tmp_path / "index"
    wheel, digest = core_fakes.plugin_wheel(
        index / "simple" / "lumen-bridge", "lumen-bridge", VERSION, "lumen_bridge", template=core_fakes.WRONG_API
    )
    port = serve_index(index, names["index"], password)
    env = {**os.environ, "EVO_HUB_CORE_PLUGIN_INDEX_URL": f"http://{INDEX_USER}:{password}@127.0.0.1:{port}/simple"}
    built = docker(
        "build", "--progress=plain", "--network", "host", "-f", str(DOCKERFILE),
        "--build-arg", f"EVO_HUB_EXTRA_CORE_PLUGINS=lumen-bridge=={VERSION} --hash=sha256:{digest}",
        "--secret", "id=core_plugin_index,env=EVO_HUB_CORE_PLUGIN_INDEX_URL",
        "-t", names["image"], str(ROOT),
        timeout=1800, env=env, check=False,
    )  # fmt: skip
    build_log = built.stdout + built.stderr
    assert built.returncode != 0, build_log[-6000:]
    assert f"entry point lumen-bridge (lumen_bridge:manifest) of lumen-bridge {VERSION} does not load:" in build_log
    assert "1 problem(s) with the core plugins of this image" in build_log
    assert docker("image", "inspect", names["image"], check=False).returncode != 0, "no image of a failed build"
