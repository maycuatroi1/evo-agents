"""Write a fictional archive to upload as a graph source (plan kg-upload-archive): one of each kind of file the hub's
extraction reads, from a seed, so the same seed gives the same files.

    python scripts/kg_upload_fixture.py OUT_DIR [--seed N] [--format zip|tgz]

It writes OUT_DIR/northwind-handbook.zip (or .tgz) and prints its path. The archive holds markdown, text, CSV, TSV,
xlsx, JSON, JSON Lines, a PDF with a text layer, a PDF without one, docx, pptx, html, a PNG, a nested zip, and files
with Vietnamese names, one stored the way many zip tools write it: UTF-8 bytes without the zip's UTF-8 flag. xlsx and
pptx need openpyxl and python-pptx (the ``ingest`` extra); without them those two files are left out.

Every name and figure is made up. PDF_SENTENCE is only in the PDF and DOCX_SENTENCE only in the docx, so a search can
show each was read. The tests (tests/kg/test_extract_office.py, tests/hub/test_kg_upload_extract.py) import it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import random
import struct
import sys
import tarfile
import zipfile
import zlib
from pathlib import Path

PDF_SENTENCE = "The lumen harbor ferry leaves pier nine at seven bells."
DOCX_SENTENCE = "Northwind keeps the spare lanterns in the eastern shed."
PPTX_SENTENCE = "Quarterly lantern drills move to the north quay."
HTML_SENTENCE = "Visitors sign the lumenops.example ledger at the gate."
NESTED_SENTENCE = "The inner archive notes the tide tables for Lumen Bay."
VIETNAMESE_NAME = "tài liệu/hướng dẫn vận hành.md"
VIETNAMESE_FLAGLESS = "tài liệu/ghi chú.txt"
VIETNAMESE_SENTENCE = "Kho đèn lồng của Northwind mở cửa lúc bảy giờ sáng."
FIXED = (2026, 1, 15, 9, 30, 0)
FIXED_TIME = dt.datetime(*FIXED, tzinfo=dt.UTC)
CITIES = ["Lumen Bay", "North Quay", "Harbor Point", "Eastwatch", "Saltmere"]
ITEMS = ["lantern", "rope", "buoy", "compass", "ledger", "oar"]


def pdf(lines: list[str]) -> bytes:
    """A one-page PDF; with ``lines``, a text layer in Helvetica, without, a filled square only."""
    if lines:
        shown = " ".join(f"({line}) Tj 0 -16 Td" for line in lines)
        content = f"BT /F1 12 Tf 72 720 Td {shown} ET".encode("latin-1")
    else:
        content = b"0.2 0.4 0.6 rg 100 400 200 200 re f"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    start = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start))
    return out.getvalue()


def _zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(zipfile.ZipInfo(name, FIXED), data, zipfile.ZIP_DEFLATED)
    return out.getvalue()


def docx(paragraphs: list[str]) -> bytes:
    """A minimal Word document, written by hand: its parts are XML in a zip."""
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    doc_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    return _zip(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                f'<Override PartName="/word/document.xml" ContentType="{doc_type}"/></Types>'
            ).encode(),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{rels}" Target="word/document.xml"/></Relationships>'
            ).encode(),
            "word/document.xml": (
                f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{word}"><w:body>{body}</w:body>'
                "</w:document>"
            ).encode(),
        }
    )


def _fixed_zip(data: bytes) -> bytes:
    """A zip-based office file with every entry dated FIXED, so the same seed gives the same bytes."""
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        return _zip({info.filename: source.read(info) for info in source.infolist()})


def xlsx(rng: random.Random) -> bytes | None:
    try:
        import openpyxl
        from openpyxl.writer.excel import ExcelWriter
    except ImportError:
        return None
    workbook = openpyxl.Workbook()
    stock = workbook.active
    stock.title = "Stock"
    stock.append(["item", "depot", "count"])
    for _ in range(6):
        stock.append([rng.choice(ITEMS), rng.choice(CITIES), rng.randint(1, 400)])
    crews = workbook.create_sheet("Crews")
    crews.append(["crew", "shift"])
    for n in range(4):
        crews.append([f"crew-{n + 1}", rng.choice(["dawn", "dusk", "night"])])
    workbook.properties.created = workbook.properties.modified = FIXED_TIME.replace(tzinfo=None)
    out = io.BytesIO()
    # ExcelWriter, not workbook.save: save_workbook sets properties.modified to now, so each second gave other bytes
    ExcelWriter(workbook, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)).save()
    return _fixed_zip(out.getvalue())


def pptx() -> bytes | None:
    try:
        from pptx import Presentation
    except ImportError:
        return None
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Lantern drills"
    slide.placeholders[1].text = PPTX_SENTENCE
    props = deck.core_properties
    props.created = props.modified = FIXED_TIME.replace(tzinfo=None)
    props.last_modified_by = props.author = "lumen-writer"
    out = io.BytesIO()
    deck.save(out)
    return _fixed_zip(out.getvalue())


def png(rng: random.Random) -> bytes:
    """An 8 by 8 RGB image of random colours."""
    rows = b"".join(b"\0" + bytes(rng.randrange(256) for _ in range(24)) for _ in range(8))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def files(seed: int) -> dict[str, bytes]:
    """The archive's files by path, from ``seed``."""
    rng = random.Random(seed)
    orders = [["order", "depot", "item", "quantity"]]
    orders += [[f"NW-{1000 + n}", rng.choice(CITIES), rng.choice(ITEMS), str(rng.randint(1, 90))] for n in range(12)]
    crews = [{"crew": f"crew-{n + 1}", "lead": f"lumen-lead-{n + 1}", "depot": rng.choice(CITIES)} for n in range(5)]
    found = {
        "handbook/readme.md": (
            "# Northwind handbook\n\nThe handbook of the fictional Northwind lantern company.\n\n"
            "## Depots\n\n" + "".join(f"- {city}\n" for city in CITIES)
        ).encode(),
        "handbook/notes.txt": b"Lantern oil is ordered on the first Monday of each month.\n",
        "data/orders.csv": "".join(",".join(row) + "\n" for row in orders).encode(),
        "data/depots.tsv": "".join(f"{city}\t{rng.randint(2, 30)}\n" for city in ["depot", *CITIES]).encode(),
        "data/crews.json": json.dumps(crews, indent=1).encode(),
        "data/events.jsonl": "".join(
            json.dumps({"event": rng.choice(["drill", "delivery", "repair"]), "day": n + 1}) + "\n" for n in range(4)
        ).encode(),
        "docs/ferry.pdf": pdf(["Lumen harbor notes", PDF_SENTENCE]),
        "docs/scan.pdf": pdf([]),
        "docs/lanterns.docx": docx(["Lantern storage", DOCX_SENTENCE]),
        "docs/visitors.html": f"<html><body><h1>Visitors</h1><p>{HTML_SENTENCE}</p></body></html>".encode(),
        "images/logo.png": png(rng),
        "archive/tides.zip": _zip({"tides/summary.md": f"# Tides\n\n{NESTED_SENTENCE}\n".encode()}),
        VIETNAMESE_NAME: f"# Hướng dẫn\n\n{VIETNAMESE_SENTENCE}\n".encode(),
        VIETNAMESE_FLAGLESS: "Ghi chú của kho Lumen Bay.\n".encode(),
    }
    for name, data in (("data/stock.xlsx", xlsx(rng)), ("docs/drills.pptx", pptx())):
        if data is not None:
            found[name] = data
    return found


def without_utf8_flag(data: bytes, name: str) -> bytes:
    """``data``, a zip, with the UTF-8 flag of entry ``name`` cleared in its local header and its central directory:
    its name stays UTF-8 bytes, as tools that never set the flag write it."""
    out = bytearray(data)
    encoded = name.encode()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo(name)
    local = info.header_offset
    flags = struct.unpack_from("<H", out, local + 6)[0]
    struct.pack_into("<H", out, local + 6, flags & ~0x800)
    central = out.find(b"PK\x01\x02")
    while central != -1:
        length = struct.unpack_from("<H", out, central + 28)[0]
        if bytes(out[central + 46 : central + 46 + length]) == encoded:
            flags = struct.unpack_from("<H", out, central + 8)[0]
            struct.pack_into("<H", out, central + 8, flags & ~0x800)
        central = out.find(b"PK\x01\x02", central + 4)
    return bytes(out)


def padding(seed: int, mib: int) -> dict[str, bytes]:
    """``mib`` MiB of random recordings from ``seed``, in files of at most 16 MiB: binary assets that make the archive
    as large as a trial upload needs."""
    rng = random.Random(seed)
    sizes = [16] * (mib // 16) + ([mib % 16] if mib % 16 else [])
    return {f"media/recording-{n + 1:03d}.bin": rng.randbytes(size << 20) for n, size in enumerate(sizes)}


def write(out_dir: Path, seed: int = 7, fmt: str = "zip", pad_mib: int = 0) -> Path:
    """Write the archive of ``seed`` into ``out_dir``, as a zip or a tgz, with ``pad_mib`` MiB of binary padding; its
    path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    found = files(seed) | padding(seed, pad_mib)
    if fmt == "zip":
        path = out_dir / "northwind-handbook.zip"
        path.write_bytes(without_utf8_flag(_zip(found), VIETNAMESE_FLAGLESS))
        return path
    path = out_dir / "northwind-handbook.tgz"
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as packed:
        with tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, data in found.items():
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(data), int(FIXED_TIME.timestamp()), 0o644
                archive.addfile(info, io.BytesIO(data))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--format", choices=["zip", "tgz"], default="zip")
    parser.add_argument("--pad-mib", type=int, default=0, help="MiB of random binary files to add (a large trial)")
    args = parser.parse_args(argv)
    print(write(args.out_dir, args.seed, args.format, args.pad_mib))
    return 0


if __name__ == "__main__":
    sys.exit(main())
