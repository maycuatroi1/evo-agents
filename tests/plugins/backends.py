"""What the tests of isolated plugins' backends share: the fixture plugins under tests/fixtures/plugins, their bundles,
an ``initialize`` payload, the hub's secrets the hostile plugin must not reach, a wheel built on the spot, and the
directory a host places plugins in (``plugin_root``)."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from evo_agents.plugins.bundle import pack
from evo_agents.plugins.manifest import read_manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"
HUB_SECRETS = {
    "EVO_HUB_DSN": "postgresql://hub:evo-hub-secret-dsn@db/hub",
    "EVO_HUB_SECRETS_KEY": "SECRETKEY-evo-hub-secret-sealing-key-000000",
    "EVO_HUB_S3_SECRET_ACCESS_KEY": "evo-hub-secret-s3-key",
    "EVO_HUB_PLUGIN_HOST_TOKEN": "evo-hub-secret-host-token-0123456789abcdef",
}
SIBLING_SECRET = "evo-sibling-secret-value"
ROOT_VARIABLE = "EVO_PLUGIN_HOST_TEST_ROOT"  # where a root test places plugins: the CI job mounts a fresh volume there


def as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def manifest(name: str, directory: Path | None = None):
    return read_manifest(((directory or FIXTURES / name) / "evo-plugin.json").read_bytes())


def bundle(name: str, directory: Path | None = None) -> bytes:
    return pack(directory or FIXTURES / name).data


def init(name: str, plugin_id: int = 1, config: dict | None = None) -> dict:
    return {
        "protocol": "plugin/1",
        "plugin": {"id": name, "version": "1.0.0"},
        "plugin_id": plugin_id,
        "hub": {"url": "http://127.0.0.1:9/", "token": "evp_test", "version": "0.0.0"},
        "config": {"instance": config or {}, "projects": {}},
        "grants": {},
    }


def tool(name: str) -> dict:
    return {"tool": name, "arguments": {}, "caller": {"project": "p"}}


def text(result: dict) -> str:
    return result["content"][0]["text"]


def make_root(tmp_path: Path) -> Path:
    """A directory for a host's plugins. Not as root, ``tmp_path``. As root, a directory each backend's user can pass
    through (pytest makes tmp_path's parents 0700, which another user cannot enter), under $EVO_PLUGIN_HOST_TEST_ROOT
    when set, else /var/tmp."""
    if not as_root():
        return tmp_path
    base = Path(os.environ.get(ROOT_VARIABLE) or "/var/tmp")
    root = Path(tempfile.mkdtemp(prefix="plugins-", dir=base))
    root.chmod(0o755)
    return root


def remove_root(root: Path) -> None:
    """Remove a root made by ``make_root``: as root without CAP_DAC_OVERRIDE, each directory is taken back first."""
    if not as_root():
        return

    def reclaim(path: Path) -> None:
        os.chown(path, 0, 0, follow_symlinks=False)
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)
            for child in path.iterdir():
                reclaim(child)

    try:
        reclaim(root)
    except OSError:
        pass
    shutil.rmtree(root, ignore_errors=True)


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_wheel(directory: Path, name: str, version: str, files: dict[str, str]) -> tuple[Path, str]:
    """A pure-Python wheel of ``name`` ``version`` holding ``files`` (path in site-packages -> text), written into
    ``directory``; its path and SHA-256. Fixed timestamps, so the same files give the same wheel."""
    dist = f"{name.replace('-', '_')}-{version}"
    entries = {path: content.encode() for path, content in files.items()}
    entries[f"{dist}.dist-info/METADATA"] = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    wheel_info = "Wheel-Version: 1.0\nGenerator: evo-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    entries[f"{dist}.dist-info/WHEEL"] = wheel_info.encode()
    record = "".join(f"{path},{_record_hash(data)},{len(data)}\n" for path, data in entries.items())
    entries[f"{dist}.dist-info/RECORD"] = (record + f"{dist}.dist-info/RECORD,,\n").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries.items():
            info = zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    data = buffer.getvalue()
    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / f"{dist}-py3-none-any.whl"
    wheel.write_bytes(data)
    return wheel, hashlib.sha256(data).hexdigest()


WHEELED_MAIN = '''"""A backend whose code imports a package installed from a wheel of its bundle; its ``probe``
tool answers the package's value and what the wheel's .pth file recorded about the user it first ran as."""

import json
import os
import sys

import evo_probe


def answer(message_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        tools = [{"name": "probe", "input_schema": {"type": "object"}}]
        answer(message["id"], {"protocol": "plugin/1", "plugin": message["params"]["plugin"], "tools": tools})
    elif method == "tool":
        marker = os.path.join(os.environ["HOME"], "pth-ran-as")
        seen = open(marker).read() if os.path.exists(marker) else None
        report = {"value": evo_probe.VALUE, "pth_uid": seen, "uid": os.getuid(), "module": evo_probe.__file__}
        answer(message["id"], {"content": [{"type": "text", "text": json.dumps(report)}]})
    else:
        answer(message["id"], {})
        if method == "shutdown":
            break
'''
PTH = (  # a .pth line that starts with import runs at every start of that environment's Python
    'import os; open(os.path.join(os.environ.get("HOME", "/nonexistent"), "pth-ran-as"), "w").write(str(os.getuid()))\n'
)


def wheeled_plugin(directory: Path, requirements: str | None = None) -> Path:
    """A plugin ``wheeled`` whose backend needs the package evo-probe, shipped as a wheel in backend/wheels/ with a
    .pth file that records the user Python first ran it as; ``requirements`` replaces the hashed requirements file."""
    import json

    backend = directory / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "main.py").write_text(WHEELED_MAIN)
    files = {"evo_probe/__init__.py": 'VALUE = "from-the-wheel"\n', "evo_probe.pth": PTH}
    _, sha256 = make_wheel(backend / "wheels", "evo-probe", "1.0", files)
    default = f"# the wheel in wheels/\nevo-probe==1.0 \\\n    --hash=sha256:{sha256}\n"
    (backend / "requirements.txt").write_text(default if requirements is None else requirements)
    data = {
        "id": "wheeled",
        "version": "1.0.0",
        "publisher": "acme",
        "name": "Wheeled",
        "description": "A plugin whose backend imports a package from a wheel it bundles. Tests only.",
        "engines": {"evo-agents": ">=0.16"},
        "plugin_api": 1,
        "backend": {"runtime": "python", "entry": "backend/main.py", "requirements": "backend/requirements.txt"},
        "contributes": {"mcp_tools": [{"name": "probe"}]},
    }
    (directory / "evo-plugin.json").write_text(json.dumps(data))
    return directory
