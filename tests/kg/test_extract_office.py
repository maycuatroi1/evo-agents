"""The kg/1 items of one file of an uploaded archive (evo_agents.kg.extract_office): markitdown's kinds, tables,
records, passages, and the assets with their reasons."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from evo_agents.kg import extract_office
from evo_agents.kg.protocol import finalize_item, validate_message

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "upload.northwind"
AT = "2026-01-15T09:30:00Z"


@pytest.fixture(scope="module")
def fixture():
    spec = importlib.util.spec_from_file_location("kg_upload_fixture", ROOT / "scripts" / "kg_upload_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def files(fixture):
    return fixture.files(7)


def read(path: str, data: bytes, **kwargs) -> extract_office.Read:
    def load(limit: int) -> bytes | None:
        return None if len(data) > limit else data

    return extract_office.read(SOURCE, path, len(data), AT, load, **kwargs)


def text_of(item: dict) -> str:
    return (item.get("body") or {}).get("text") or ""


def assert_valid(items: list[dict]) -> None:
    for item in items:
        assert validate_message(finalize_item({**item, "rev": "1", "label": {"level": "internal"}})) == [], item["id"]


@pytest.mark.parametrize(
    "path, kind, sentence",
    [
        ("docs/ferry.pdf", "pdf", "PDF_SENTENCE"),
        ("docs/lanterns.docx", "docx", "DOCX_SENTENCE"),
        ("docs/drills.pptx", "pptx", "PPTX_SENTENCE"),
        ("docs/visitors.html", "html", "HTML_SENTENCE"),
    ],
)
def test_documents_become_markdown_items_of_their_own_kind(fixture, files, path, kind, sentence):
    pytest.importorskip("markitdown")
    if path not in files:
        pytest.skip(f"{path} needs python-pptx")
    found = read(path, files[path])
    assert found.asset is None
    [item] = found.items
    assert item["id"] == f"{SOURCE}:file:{path}"
    assert item["kind"] == kind
    assert item["body"]["format"] == "markdown"
    assert getattr(fixture, sentence) in " ".join(text_of(item).split())
    assert item["fragments"]
    assert item["props"] == {"path": path, "bytes": len(files[path])}
    assert_valid(found.items)


def test_a_workbook_is_an_xlsx_item_and_each_sheet_a_table(files):
    pytest.importorskip("markitdown")
    if "data/stock.xlsx" not in files:
        pytest.skip("needs openpyxl")
    found = read("data/stock.xlsx", files["data/stock.xlsx"])
    workbook, *sheets = found.items
    assert workbook["kind"] == "xlsx"
    assert [sheet["id"] for sheet in sheets] == [
        f"{SOURCE}:table:data/stock.xlsx#Stock",
        f"{SOURCE}:table:data/stock.xlsx#Crews",
    ]
    assert {sheet["kind"] for sheet in sheets} == {"table"}
    assert all(sheet["parent"] == workbook["id"] for sheet in sheets)
    assert sheets[0]["props"]["rows"] > 0 and sheets[0]["props"]["columns"]
    assert_valid(found.items)


def test_csv_and_tsv_are_tables_with_their_columns_and_rows(files):
    [orders] = read("data/orders.csv", files["data/orders.csv"]).items
    assert orders["kind"] == "table"
    assert orders["props"]["columns"] == ["order", "depot", "item", "quantity"]
    assert orders["props"]["rows"] == 12
    assert "NW-1000" in text_of(orders)
    [depots] = read("data/depots.tsv", files["data/depots.tsv"]).items
    assert depots["kind"] == "table"
    assert depots["props"]["columns"][0] == "depot"
    assert_valid([orders, depots])


def test_json_and_jsonl_arrays_of_objects_give_records(files):
    crews = read("data/crews.json", files["data/crews.json"]).items
    assert crews[0]["kind"] == "json" and crews[0]["props"]["records"] == 5
    assert [item["kind"] for item in crews[1:]] == ["record"] * 5
    assert crews[1]["id"] == f"{SOURCE}:record:data/crews.json#0"
    assert crews[1]["parent"] == crews[0]["id"]
    assert json.loads(text_of(crews[1]))["crew"] == "crew-1"
    events = read("data/events.jsonl", files["data/events.jsonl"]).items
    assert [item["kind"] for item in events] == ["json"] + ["record"] * 4
    assert_valid(crews + events)


def test_json_that_is_not_an_array_of_objects_is_one_json_item():
    found = read("data/settings.json", b'{"depot": "Lumen Bay"}')
    assert [item["kind"] for item in found.items] == ["json"]


def test_markdown_and_text_keep_the_kinds_a_git_source_gives(fixture, files):
    [readme] = read("handbook/readme.md", files["handbook/readme.md"]).items
    assert readme["kind"] == "markdown" and readme["fragments"]
    [notes] = read("handbook/notes.txt", files["handbook/notes.txt"]).items
    assert notes["kind"] == "text"
    [guide] = read(fixture.VIETNAMESE_NAME, files[fixture.VIETNAMESE_NAME]).items
    assert guide["id"] == f"{SOURCE}:file:{fixture.VIETNAMESE_NAME}"
    assert fixture.VIETNAMESE_SENTENCE in text_of(guide)
    assert_valid([readme, notes, guide])


def test_long_text_is_split_into_passages_that_hold_all_of_it():
    paragraphs = [f"Paragraph {n} of the Northwind ledger. " * 40 for n in range(60)]
    text = "# Ledger\n\n" + "\n\n".join(paragraphs) + "\n"
    assert len(text) > 2 * extract_office.PASSAGE_CHARS
    [item] = read("handbook/ledger.md", text.encode()).items
    fragments = item["fragments"]
    assert len(fragments) >= 3
    assert all(len(fragment["text"]) <= extract_office.PASSAGE_CHARS for fragment in fragments)
    assert [fragment["anchor"] for fragment in fragments[:2]] == ["ledger-1", "ledger-2"]
    joined = "".join(fragment["text"] for fragment in fragments)
    assert all(f"Paragraph {n} of" in joined for n in range(60))
    assert_valid([item])


def test_a_pdf_without_text_is_an_asset_counted_as_no_text_layer(files):
    pytest.importorskip("markitdown")
    found = read("docs/scan.pdf", files["docs/scan.pdf"])
    assert found.asset == "no_text_layer"
    [item] = found.items
    assert item["kind"] == "asset"
    assert item["props"]["reason"] == "no_text_layer"
    assert item["props"]["media_type"] == "application/pdf"
    assert "body" not in item


def test_binaries_large_files_and_slow_files_are_assets_by_reason(files, monkeypatch):
    assert read("images/logo.png", files["images/logo.png"]).asset == "binary"
    assert read("data/latin1.txt", "café".encode("latin-1")).asset == "binary"

    def never(limit: int) -> bytes:
        raise AssertionError("a file over the limit is not read")

    large = extract_office.read(SOURCE, "data/huge.csv", extract_office.MAX_FILE_BYTES + 1, AT, never)
    assert large.asset == "too_large"
    assert large.items[0]["props"]["bytes"] == extract_office.MAX_FILE_BYTES + 1

    def slow(*args):
        time.sleep(2)
        return []

    monkeypatch.setattr(extract_office, "_items", slow)
    timed_out = read("handbook/notes.txt", b"slow", seconds=0.05)
    assert timed_out.asset == "timeout"
    assert timed_out.items[0]["props"]["reason"] == "timeout"


def test_a_broken_document_is_an_asset_counted_as_unreadable():
    pytest.importorskip("markitdown")
    found = read("docs/broken.docx", b"PK\x03\x04 this is not a document")
    assert found.asset == "unreadable"
    assert found.items[0]["kind"] == "asset"


def test_without_markitdown_documents_are_unreadable_assets(files, monkeypatch):
    import builtins

    real = builtins.__import__

    def no_markitdown(name, *args, **kwargs):
        if name.startswith("markitdown"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_markitdown)
    assert read("docs/ferry.pdf", files["docs/ferry.pdf"]).asset == "unreadable"
