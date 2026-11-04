"""Reading an uploaded archive entry by entry (evo_agents.kg.archive): unsafe paths refused per entry, links, devices
and encrypted entries skipped, the limits, zip bombs, nesting, names in UTF-8 and cp437, and a fixed order."""

from __future__ import annotations

import importlib.util
import io
import stat
import struct
import tarfile
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from evo_agents.kg import archive
from evo_agents.kg.archive import Limits, safe_path, walk

ROOT = Path(__file__).resolve().parents[2]


def fixture_module():
    spec = importlib.util.spec_from_file_location("kg_upload_fixture", ROOT / "scripts" / "kg_upload_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def zip_bytes(entries: list[tuple[str | zipfile.ZipInfo, bytes]], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as packed:
        for name, data in entries:
            packed.writestr(name, data)
    return buffer.getvalue()


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def read_all(path: Path, limits: Limits | None = None, spool: Path | None = None, noted: list | None = None):
    skipped: Counter = Counter()
    found = {
        entry.path: entry.read(10**9)
        for entry in walk(path, skipped, limits=limits or Limits(), spool=spool, noted=noted)
    }
    return found, skipped


def special(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, (2026, 1, 15, 9, 30, 0))
    info.external_attr = mode << 16
    return info


@pytest.mark.parametrize(
    "name, expected",
    [
        ("docs/readme.md", "docs/readme.md"),
        ("./docs//readme.md", "docs/readme.md"),
        ("docs\\windows.txt", "docs/windows.txt"),
        ("/etc/passwd", None),
        ("../outside.txt", None),
        ("docs/../../outside.txt", None),
        ("C:/windows/win.ini", None),
        ("bad\nname.txt", None),
        ("", None),
    ],
)
def test_safe_path_refuses_absolute_paths_dot_dot_drives_and_control_characters(name, expected):
    assert safe_path(name) == expected


def test_unsafe_entries_are_refused_one_by_one_and_the_rest_is_read(tmp_path):
    data = zip_bytes(
        [
            ("first.txt", b"one"),
            ("/abs/path.txt", b"x"),
            ("../escape.txt", b"x"),
            ("a/../../escape.txt", b"x"),
            ("last.txt", b"two"),
        ]
    )
    found, skipped = read_all(write(tmp_path, "upload.zip", data))
    assert found == {"first.txt": b"one", "last.txt": b"two"}
    assert skipped == Counter({"unsafe_path": 3})


def set_encrypted(data: bytes, name: str) -> bytes:
    """``data``, a zip, with the encryption flag of entry ``name`` set in its local header and central directory,
    which zipfile does not write itself."""
    out = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as packed:
        local = packed.getinfo(name).header_offset
    struct.pack_into("<H", out, local + 6, struct.unpack_from("<H", out, local + 6)[0] | 0x1)
    central = out.find(b"PK\x01\x02")
    while central != -1:
        length = struct.unpack_from("<H", out, central + 28)[0]
        if bytes(out[central + 46 : central + 46 + length]) == name.encode():
            struct.pack_into("<H", out, central + 8, struct.unpack_from("<H", out, central + 8)[0] | 0x1)
        central = out.find(b"PK\x01\x02", central + 4)
    return bytes(out)


def test_links_devices_and_encrypted_entries_of_a_zip_are_skipped_by_reason(tmp_path):
    encrypted = "secret.txt"
    data = zip_bytes(
        [
            ("kept.txt", b"kept"),
            (special("link", stat.S_IFLNK | 0o777), b"/etc/passwd"),
            (special("tty", stat.S_IFCHR | 0o600), b""),
            (special("pipe", stat.S_IFIFO | 0o600), b""),
            (encrypted, b"not really encrypted"),
        ],
        zipfile.ZIP_STORED,
    )
    data = set_encrypted(data, encrypted)
    found, skipped = read_all(write(tmp_path, "upload.zip", data))
    assert list(found) == ["kept.txt"]
    assert skipped == Counter({"symlink": 1, "device": 2, "encrypted": 1})


def test_links_and_devices_of_a_tar_are_skipped_and_a_tar_is_read_as_one_stream(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as packed:
        for name, data in (("a.txt", b"alpha"), ("../up.txt", b"x"), ("b.txt", b"beta")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
        for name, kind in (("sym", tarfile.SYMTYPE), ("hard", tarfile.LNKTYPE), ("dev", tarfile.CHRTYPE)):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, "a.txt"
            packed.addfile(info)
        fifo = tarfile.TarInfo("fifo")
        fifo.type = tarfile.FIFOTYPE
        packed.addfile(fifo)
    found, skipped = read_all(write(tmp_path, "upload.tar.gz", buffer.getvalue()))
    assert found == {"a.txt": b"alpha", "b.txt": b"beta"}
    assert skipped == Counter({"unsafe_path": 1, "symlink": 1, "link": 1, "device": 2})


def test_a_zip_bomb_entry_is_never_decompressed_but_a_small_compressible_one_is_read(tmp_path):
    bomb = b"\0" * (4 * 1024 * 1024)
    data = zip_bytes([("bomb.bin", bomb), ("small.txt", b"a" * 10_000)])
    found, skipped = read_all(write(tmp_path, "upload.zip", data))
    assert list(found) == ["small.txt"]
    assert skipped == Counter({"ratio": 1})


def test_entries_past_the_entry_and_size_limits_are_counted(tmp_path):
    data = zip_bytes([(f"f{n}.txt", b"x" * 10) for n in range(5)])
    path = write(tmp_path, "upload.zip", data)
    found, skipped = read_all(path, Limits(max_entries=3))
    assert list(found) == ["f0.txt", "f1.txt", "f2.txt"]
    assert skipped == Counter({"too_many_entries": 2})
    found, skipped = read_all(path, Limits(max_total=25))
    assert list(found) == ["f0.txt", "f1.txt"]
    assert skipped == Counter({"total_size": 3})


def test_nested_archives_open_to_depth_two_and_deeper_ones_are_skipped(tmp_path):
    third = zip_bytes([("deep.txt", b"three levels down")])
    second = zip_bytes([("two.txt", b"two"), ("inner/third.zip", third)])
    first = zip_bytes([("one.txt", b"one"), ("second.zip", second)])
    data = zip_bytes([("top.txt", b"top"), ("nested/first.zip", first)])
    found, skipped = read_all(write(tmp_path, "upload.zip", data), spool=tmp_path)
    assert found == {
        "top.txt": b"top",
        "nested/first.zip/one.txt": b"one",
        "nested/first.zip/second.zip/two.txt": b"two",
    }
    assert skipped == Counter({"depth": 1})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["upload.zip"]  # spooled archives are gone


def test_a_nested_tar_inside_a_zip_and_a_corrupt_nested_archive(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as packed:
        info = tarfile.TarInfo("inside.md")
        info.size = 5
        packed.addfile(info, io.BytesIO(b"# hi\n"))
    data = zip_bytes([("bundle.tar", buffer.getvalue()), ("broken.zip", b"not an archive at all")])
    found, skipped = read_all(write(tmp_path, "upload.zip", data))
    assert found == {"bundle.tar/inside.md": b"# hi\n"}
    assert skipped == Counter({"corrupt": 1})


def test_a_corrupt_upload_is_counted_and_yields_nothing(tmp_path):
    found, skipped = read_all(write(tmp_path, "upload.zip", b"PK\x03\x04 truncated"))
    assert found == {}
    assert skipped == Counter({"corrupt": 1})


def patch_name(data: bytes, placeholder: bytes, raw: bytes) -> bytes:
    """``data`` with every copy of the entry name ``placeholder`` replaced by ``raw`` of the same length."""
    assert len(placeholder) == len(raw)
    return data.replace(placeholder, raw)


def test_a_name_without_the_utf8_flag_is_read_as_cp437_or_as_the_utf8_it_is(tmp_path):
    data = zip_bytes([("caf_.txt", b"cp437 name"), ("ok.txt", b"plain")], zipfile.ZIP_STORED)
    data = patch_name(data, b"caf_.txt", b"caf\x82.txt")  # 0x82 is e acute in cp437
    found, _ = read_all(write(tmp_path, "cp437.zip", data))
    assert set(found) == {"café.txt", "ok.txt"}

    module = fixture_module()
    utf8 = module.without_utf8_flag(zip_bytes([(module.VIETNAMESE_FLAGLESS, b"vi")]), module.VIETNAMESE_FLAGLESS)
    with zipfile.ZipFile(io.BytesIO(utf8)) as packed:
        assert not packed.infolist()[0].flag_bits & 0x800
    found, _ = read_all(write(tmp_path, "flagless.zip", utf8))
    assert list(found) == [module.VIETNAMESE_FLAGLESS]


def test_vietnamese_names_are_put_in_nfc(tmp_path):
    name = "tài liệu/hướng dẫn.md"
    decomposed = unicodedata.normalize("NFD", name)
    found, _ = read_all(write(tmp_path, "vi.zip", zip_bytes([(decomposed, b"x")])))
    assert list(found) == [unicodedata.normalize("NFC", name)]


def test_a_second_entry_of_the_same_path_is_a_duplicate_and_the_first_is_kept(tmp_path):
    with pytest.warns(UserWarning):
        data = zip_bytes([("same.txt", b"first"), ("same.txt", b"second")])
    found, skipped = read_all(write(tmp_path, "upload.zip", data))
    assert found == {"same.txt": b"first"}
    assert skipped == Counter({"duplicate": 1})


def test_noted_paths_name_what_was_skipped_and_stop_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_NOTED", 2)
    data = zip_bytes([("../a", b"x"), ("../b", b"x"), ("../c", b"x"), ("kept.txt", b"y")])
    noted: list = []
    _, skipped = read_all(write(tmp_path, "upload.zip", data), noted=noted)
    assert skipped == Counter({"unsafe_path": 3})
    assert noted == [{"path": "../a", "reason": "unsafe_path"}, {"path": "../b", "reason": "unsafe_path"}]


def test_an_entry_read_past_its_limit_gives_none(tmp_path):
    path = write(tmp_path, "upload.zip", zip_bytes([("big.txt", b"x" * 100)]))
    entries = walk(path, Counter())
    entry = next(entries)
    assert entry.read(99) is None
    assert entry.read(100) == b"x" * 100


def test_the_fixture_reads_the_same_as_zip_and_tgz_in_a_fixed_order(tmp_path):
    module = fixture_module()
    first = module.write(tmp_path / "a", seed=7)
    again = module.write(tmp_path / "b", seed=7)
    assert first.read_bytes() == again.read_bytes()
    tgz = module.write(tmp_path / "c", seed=7, fmt="tgz")
    zipped, zip_skipped = read_all(first, spool=tmp_path)
    tarred, tar_skipped = read_all(tgz, spool=tmp_path)
    assert zipped == tarred
    assert list(zipped) == list(read_all(first, spool=tmp_path)[0])
    assert zip_skipped == tar_skipped == Counter()
    assert module.VIETNAMESE_NAME in zipped and module.VIETNAMESE_FLAGLESS in zipped
    assert "archive/tides.zip/tides/summary.md" in zipped
    entries = walk(first, Counter())
    entry = next(entries)
    assert entry.mtime.startswith("2026-01-15T09:30")
