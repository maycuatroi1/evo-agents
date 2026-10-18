"""Plugin bundles: packing a directory gives the same bytes every time, keeps the execute bit and nothing else of a
file's metadata, and leaves out what a checkout leaves behind; reading a bundle checks it whole, refusing whatever
could land outside the plugin's directory or mislead, and what is too large."""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import tarfile

import pytest

from evo_agents.plugins import bundle as plugin_bundle
from evo_agents.plugins.bundle import KIND, MAX_BUNDLE, BundleError, compress, pack, read_bundle
from tests.plugins.samples import manifest, write_plugin

MANIFEST = plugin_bundle.MANIFEST_FILE


def test_plugin_bundle_packs_one_directory_into_the_same_bytes_every_time(tmp_path):
    first = write_plugin(tmp_path / "one", files={"web/index.html": "<p>hi</p>", "backend/run.sh": "#!/bin/sh\n"})
    second = write_plugin(tmp_path / "two", files={"backend/run.sh": "#!/bin/sh\n", "web/index.html": "<p>hi</p>"})
    if os.name != "nt":
        (first / "backend" / "run.sh").chmod(0o700)
        (second / "backend" / "run.sh").chmod(0o751)
        os.utime(second / "web" / "index.html", (0, 0))
    for skipped in (".git/HEAD", "backend/__pycache__/x.cpython-312.pyc", ".DS_Store", "backend/old.pyc"):
        (second / skipped).parent.mkdir(parents=True, exist_ok=True)
        (second / skipped).write_bytes(b"left out")
    packed, again = pack(first), pack(second)
    assert packed.data == again.data and packed.sha256 == again.sha256
    assert KIND == "plugin-bundle" and len(packed.data) < MAX_BUNDLE
    contents = read_bundle(packed.data)
    assert [entry.path for entry in contents.files] == ["backend/run.sh", MANIFEST, "web/index.html"]
    assert contents.manifest == packed.manifest and contents.manifest.id == "sandbox"
    with tarfile.open(fileobj=io.BytesIO(packed.data), mode="r:gz") as tar:
        members = {member.name: member for member in tar.getmembers()}
    assert {(m.mtime, m.uid, m.gid, m.uname, m.gname) for m in members.values()} == {(0, 0, 0, "", "")}
    # Executable by its mode on POSIX, by its #! line on Windows, which has no execute bit (bundle._read_file).
    assert members["backend/run.sh"].mode == 0o755
    assert members["web/index.html"].mode == 0o644


def test_plugin_bundle_packing_refuses_links_a_missing_manifest_and_a_wrong_one(tmp_path):
    with pytest.raises(BundleError, match="is not a directory"):
        pack(tmp_path / "nowhere")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BundleError, match=f"has no {MANIFEST} at its top"):
        pack(empty)
    with pytest.raises(BundleError, match=f"{MANIFEST} is not a plugin manifest: version:"):
        pack(write_plugin(tmp_path / "wrong", manifest(version="1")))
    linked = write_plugin(tmp_path / "linked")
    try:
        (linked / "web" / "link.html").symlink_to(linked / "web" / "index.html")
    except OSError:  # Windows without the privilege to make links
        pytest.skip("this machine cannot make symbolic links")
    with pytest.raises(BundleError, match="is not a regular file"):
        pack(linked)


def entry(name: str, data: bytes = b"x", kind: bytes = tarfile.REGTYPE, linkname: str = "") -> tuple:
    return name, data, kind, linkname


def tarball(*entries: tuple, with_manifest: bool = True, gzipped: bool = True) -> bytes:
    """A tar of ``entries`` as they are, after an evo-plugin.json unless ``with_manifest`` is false."""
    buffer = io.BytesIO()
    items = ([entry(MANIFEST, json.dumps(manifest()).encode())] if with_manifest else []) + list(entries)
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data, kind, linkname in items:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, linkname
            if kind in (tarfile.REGTYPE, tarfile.AREGTYPE):
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return gzip.compress(buffer.getvalue(), mtime=0) if gzipped else buffer.getvalue()


# Each case's id is its problem: an id made of the bundle's bytes holds NUL characters and runs long, and pytest puts
# the id in the variable PYTEST_CURRENT_TEST, which Windows refuses then.
REFUSED = [
    (b"not a tarball", "not a gzip-compressed tar"),
    (tarball(entry("web/a.html"), gzipped=False), "not a gzip-compressed tar"),
    (tarball(entry("/etc/passwd")), "an absolute path"),
    (tarball(entry("../outside")), "an empty, '.' or '..' part"),
    (tarball(entry("web/./a")), "an empty, '.' or '..' part"),
    (tarball(entry("web\\a")), "a backslash"),
    (tarball(entry("web/\x07bell")), "a control character"),
    (tarball(entry("web/" + "a" * 256)), "a part over 255 bytes"),
    (tarball(entry("web/link", kind=tarfile.SYMTYPE, linkname="/etc/passwd")), "is not a regular file"),
    (tarball(entry("web/hard", kind=tarfile.LNKTYPE, linkname=MANIFEST)), "is not a regular file"),
    (tarball(entry("web/fifo", kind=tarfile.FIFOTYPE)), "is not a regular file"),
    (tarball(entry("web/A.html"), entry("web/a.html")), "one path ignoring case"),
    (tarball(entry("web"), entry("web/a.html")), "which is a file"),
    (tarball(entry("web/a.html"), with_manifest=False), f"has no {MANIFEST} at its top"),
    (tarball(entry(f"nested/{MANIFEST}"), with_manifest=False), f"has no {MANIFEST} at its top"),
]


@pytest.mark.parametrize(("data", "problem"), REFUSED, ids=[problem for _, problem in REFUSED])
def test_plugin_bundle_is_refused_whole_for_any_entry_that_could_land_outside_or_mislead(data, problem):
    with pytest.raises(BundleError, match=re.escape(problem)):
        read_bundle(data)


def test_plugin_bundle_reading_is_bounded_in_bytes_and_in_entries(monkeypatch):
    with pytest.raises(BundleError, match="over 32 MiB"):
        read_bundle(b"\0" * (MAX_BUNDLE + 1))
    monkeypatch.setattr(plugin_bundle, "MAX_FILES", 3)
    with pytest.raises(BundleError, match="over 3 entries"):
        read_bundle(tarball(entry("a"), entry("b"), entry("c")))
    monkeypatch.setattr(plugin_bundle, "MAX_FILES", 10_000)
    monkeypatch.setattr(plugin_bundle, "MAX_UNPACKED", 4096)
    with pytest.raises(BundleError, match="over 0 MiB together"):
        read_bundle(tarball(entry("big", b"x" * 8192)))
    monkeypatch.setattr(plugin_bundle, "TAR_OVERHEAD", 0)
    with pytest.raises(BundleError, match="decompresses to over"):  # headers count too, not only the files
        read_bundle(tarball(*(entry(f"f{index}", b"") for index in range(20))))


def test_plugin_bundle_written_by_compress_reads_back(tmp_path):
    packed = pack(write_plugin(tmp_path / "plugin"))
    assert compress(read_bundle(packed.data).files) == packed.data


def test_plugin_bundle_holds_the_entry_of_each_page_it_names(tmp_path):
    page = {"name": "board", "entry": "pages/board.html"}
    data = manifest(contributes={"pages": [page]})
    held = pack(write_plugin(tmp_path / "held", data, {"pages/board.html": "<p>board</p>\n", "pages/app.js": "1;\n"}))
    assert [entry.path for entry in read_bundle(held.data).files][-2:] == ["pages/app.js", "pages/board.html"]
    with pytest.raises(BundleError, match="page board names pages/board.html, which the bundle does not hold"):
        pack(write_plugin(tmp_path / "missing", data, {"pages/index.html": "<p>other</p>\n"}))
    missing = compress([plugin_bundle.Entry(MANIFEST, False, json.dumps(data).encode())])
    with pytest.raises(BundleError, match="which the bundle does not hold"):
        read_bundle(missing)
