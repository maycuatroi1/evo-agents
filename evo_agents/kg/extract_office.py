"""The kg/1 items of one file of an uploaded archive (``evo_agents.kg.archive``), as the hub's extraction of an upload
(``evo_agents.hub.kg_upload_extract``) gives them to the source of the upload.

Each file is one item, ``<source>:file:<path>``, titled by its path, with ``props.path`` and ``props.bytes``:

- PDF with a text layer, docx, xlsx, pptx and html: markdown, through markitdown (the ``ingest`` extra), of kind
  ``pdf``, ``docx``, ``xlsx``, ``pptx`` or ``html``;
- markdown, text, code and config files: their text, of the kind a git source gives them (``connectors._files``);
- CSV and TSV: kind ``table``, with ``props.columns`` and ``props.rows``; each sheet of an xlsx is also an item of kind
  ``table``, ``<source>:table:<path>#<sheet>``, whose parent is the workbook;
- JSON and JSON Lines holding objects: kind ``json``, and each object (at most MAX_RECORDS of a file) an item of kind
  ``record``, ``<source>:record:<path>#<n>``, whose parent is the file.

A document's markdown is cut into sections at its headings, and a section longer than PASSAGE_CHARS into passages
(``kg.text.split_passages``), each a fragment, so the whole of a long text is searched.

What cannot be read this way is an asset (path, size and media type, never its bytes), and ``read`` says why: a PDF
without a text layer (``no_text_layer``), a file over MAX_FILE_BYTES (``too_large``), another binary file
(``binary``), a file whose reading took longer than ``seconds`` (``timeout``), and a file the converter failed on or
that the ``ingest`` extra is needed for (``unreadable``). No OCR, and no content of an image, audio or video.
"""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from evo_agents.kg.connectors._files import KIND_BY_SUFFIX, LANGUAGE_BY_SUFFIX
from evo_agents.kg.text import split_markdown, split_passages

MiB = 1024 * 1024
MAX_FILE_BYTES = 64 * MiB  # a larger file is an asset
READ_SECONDS = 120.0  # what reading one file may take
PASSAGE_CHARS = 20_000  # a section longer than this is cut into passages
MAX_RECORDS = 10_000  # records of one JSON file
MAX_TABLE_CHARS = 1_000_000  # of a table's text; its rows are all counted
MARKITDOWN = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx", ".html": "html", ".htm": "html"}
TABLES = {".csv": ",", ".tsv": "\t"}
RECORDS = {".json", ".jsonl", ".ndjson"}
TEXT_KINDS = {suffix: kind for suffix, kind in KIND_BY_SUFFIX.items() if suffix not in MARKITDOWN}
MARKDOWN_KINDS = {"markdown", *MARKITDOWN.values()}


class Unreadable(Exception):
    """A file that is an asset; its message is the reason."""


@dataclass(frozen=True)
class Read:
    """The items of one file, and why it is an asset when it is one (None when it was read)."""

    items: list[dict]
    asset: str | None = None


def media_type(path: str) -> str:
    return mimetypes.guess_type(path, strict=False)[0] or "application/octet-stream"


def _base(source: str, path: str, size: int, rev_time: str, kind: str) -> dict:
    return {
        "id": f"{source}:file:{path}",
        "kind": kind,
        "rev_time": rev_time,
        "title": path,
        "props": {"path": path, "bytes": size},
    }


def asset(source: str, path: str, size: int, rev_time: str, reason: str) -> dict:
    """The item of a file known by its path, size and media type only."""
    item = _base(source, path, size, rev_time, "asset")
    item["props"] = {**item["props"], "media_type": media_type(path), "reason": reason}
    return item


def fragments(text: str) -> list[dict]:
    """``text``'s sections at its headings, each longer than PASSAGE_CHARS cut into passages."""
    found = []
    for section in split_markdown(text):
        if len(section["text"]) <= PASSAGE_CHARS:
            found.append(section)
            continue
        first = int(section["span"].split()[1].split("-")[0])
        for n, passage in enumerate(split_passages(section["text"], first, PASSAGE_CHARS), start=1):
            part = {**section, "anchor": f"{section['anchor']}-{n}", "text": passage["text"]}
            part["span"] = f"lines {passage['start']}-{passage['end']}"
            if n > 1:
                part.pop("title", None)
            found.append(part)
    return found


def _decode(data: bytes) -> str:
    if b"\0" in data[:8192]:
        raise Unreadable("binary")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Unreadable("binary") from None


def _converter(suffix: str):
    try:
        from markitdown import StreamInfo
        from markitdown.converters import DocxConverter, HtmlConverter, PdfConverter, PptxConverter, XlsxConverter
    except ImportError:  # the ingest extra is not installed
        raise Unreadable("unreadable") from None
    kind = MARKITDOWN[suffix]
    converter = {
        "pdf": PdfConverter,
        "docx": DocxConverter,
        "xlsx": XlsxConverter,
        "pptx": PptxConverter,
        "html": HtmlConverter,
    }[kind]()
    return converter, StreamInfo(extension=suffix, charset="utf-8" if kind == "html" else None)


def _markdown(suffix: str, data: bytes) -> str:
    converter, info = _converter(suffix)
    try:
        text = converter.convert(io.BytesIO(data), info).markdown or ""
    except Exception:  # a converter fails in its own ways on a broken file
        raise Unreadable("unreadable") from None
    if not text.strip():
        raise Unreadable("no_text_layer" if suffix == ".pdf" else "unreadable")
    return text.replace("\r\n", "\n")


def _table_text(columns: list[str], rows: list[list[str]]) -> str:
    lines = [" | ".join(columns)]
    size = len(lines[0])
    for row in rows:
        line = " | ".join(row)
        size += len(line) + 1
        if size > MAX_TABLE_CHARS:
            break
        lines.append(line)
    return "\n".join(lines) + "\n"


def _cells(row) -> list[str]:
    return ["" if cell is None else str(cell) for cell in row]


def _table(item: dict, columns: list[str], rows: list[list[str]]) -> dict:
    width = max([len(columns), *(len(row) for row in rows)], default=0)
    named = columns + [f"column {n}" for n in range(len(columns) + 1, width + 1)]
    item["body"] = {"format": "text", "text": _table_text(named, rows)}
    item["props"] = {**item["props"], "columns": named, "rows": len(rows)}
    return item


def _csv(base: dict, text: str, delimiter: str) -> list[dict]:
    rows = [_cells(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    columns, rows = (rows[0], rows[1:]) if rows else ([], [])
    return [_table({**base, "kind": "table"}, columns, rows)]


def _sheets(source: str, path: str, data: bytes, base: dict) -> list[dict]:
    try:
        import openpyxl
    except ImportError:
        return []
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # a workbook markitdown read but openpyxl does not: its markdown stands
        return []
    found = []
    try:
        for sheet in workbook.worksheets:
            rows = [_cells(row) for row in sheet.iter_rows(values_only=True)]
            rows = [row for row in rows if any(cell for cell in row)]
            columns, rows = (rows[0], rows[1:]) if rows else ([], [])
            item = {
                "id": f"{source}:table:{path}#{sheet.title}",
                "kind": "table",
                "rev_time": base["rev_time"],
                "title": f"{path} {sheet.title}",
                "parent": base["id"],
                "props": {"path": path, "sheet": sheet.title},
            }
            found.append(_table(item, columns, rows))
    finally:
        workbook.close()
    return found


def _objects(suffix: str, text: str) -> list | None:
    """The objects of a JSON array or a JSON Lines file; None when it holds anything else."""
    try:
        if suffix == ".json":
            found = json.loads(text)
        else:
            found = [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        return None
    if not isinstance(found, list) or not found or not all(isinstance(entry, dict) for entry in found):
        return None
    return found


def _records(source: str, path: str, suffix: str, text: str, base: dict) -> list[dict]:
    file_item = {**base, "kind": "json", "body": {"format": "text", "text": text}}
    objects = _objects(suffix, text)
    if objects is None:
        return [file_item]
    file_item["props"] = {**base["props"], "records": len(objects)}
    found = [file_item]
    for n, entry in enumerate(objects[:MAX_RECORDS]):
        found.append(
            {
                "id": f"{source}:record:{path}#{n}",
                "kind": "record",
                "rev_time": base["rev_time"],
                "title": f"{path} #{n}",
                "parent": base["id"],
                "body": {"format": "text", "text": json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=1)},
                "props": {"path": path, "index": n, "fields": sorted(str(key) for key in entry)},
            }
        )
    return found


def _text(base: dict, suffix: str, text: str) -> list[dict]:
    kind = TEXT_KINDS.get(suffix, "text")
    body_format = {"markdown": "markdown", "yaml": "yaml", "code": "code"}.get(kind, "text")
    item = {**base, "kind": kind, "body": {"format": body_format, "text": text}}
    if suffix in LANGUAGE_BY_SUFFIX:
        item["body"]["language"] = LANGUAGE_BY_SUFFIX[suffix]
    if kind == "markdown":
        item["fragments"] = fragments(text)
    return [item]


def _items(source: str, path: str, data: bytes, rev_time: str) -> list[dict]:
    """The items of a file whose bytes are ``data``; Unreadable when it is an asset."""
    suffix = PurePosixPath(path).suffix.lower()
    base = _base(source, path, len(data), rev_time, "text")
    if suffix in MARKITDOWN:
        text = _markdown(suffix, data)
        item = {**base, "kind": MARKITDOWN[suffix], "body": {"format": "markdown", "text": text}}
        item["fragments"] = fragments(text)
        return [item] + (_sheets(source, path, data, base) if suffix == ".xlsx" else [])
    text = _decode(data)
    if suffix in TABLES:
        return _csv(base, text, TABLES[suffix])
    if suffix in RECORDS:
        return _records(source, path, suffix, text, base)
    return _text(base, suffix, text)


def _within(seconds: float, work: Callable[[], list[dict]]) -> list[dict]:
    """``work()``, run on a thread of its own; Unreadable("timeout") when it takes longer than ``seconds``. A converter
    cannot be stopped from outside: the thread is left to end on its own, and what it gives is dropped."""
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["items"] = work()
        except BaseException as exc:  # handed to the caller's thread as is
            outcome["error"] = exc

    thread = threading.Thread(target=run, name="kg-extract", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise Unreadable("timeout")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["items"]


def read(
    source: str, path: str, size: int, rev_time: str, load: Callable[[int], bytes | None], *, seconds=READ_SECONDS
) -> Read:
    """The items of the file at ``path`` of ``size`` bytes, last changed at ``rev_time``, as source ``source`` gives
    them; ``load(limit)`` gives its bytes, or None past ``limit``. An asset when it cannot be read (see the module)."""
    data = load(MAX_FILE_BYTES) if size <= MAX_FILE_BYTES else None
    if data is None:
        return Read([asset(source, path, size, rev_time, "too_large")], "too_large")
    try:
        return Read(_within(seconds, lambda: _items(source, path, data, rev_time)))
    except Unreadable as exc:
        reason = str(exc)
        return Read([asset(source, path, size, rev_time, reason)], reason)


__all__ = ["MAX_FILE_BYTES", "MAX_RECORDS", "PASSAGE_CHARS", "Read", "asset", "fragments", "media_type", "read"]
