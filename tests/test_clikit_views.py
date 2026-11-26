"""The views of evo_agents.clikit: where each part of a result is written."""

from __future__ import annotations

import io

from evo_agents.clikit import Result, Stderr, Table, Text, Verbatim, Views


def test_a_stderr_part_of_views_goes_to_stderr_in_its_place(capsys):
    out = io.StringIO()
    Result({"id": 1}, Views(Text("queued"), Stderr(Text("Warning: none can take it")), Text("follow it"))).show(
        out, as_json=False
    )
    assert out.getvalue() == "queued\nfollow it\n"
    assert capsys.readouterr().err == "Warning: none can take it\n"


def test_json_prints_the_data_alone(capsys):
    out = io.StringIO()
    Result({"id": 1}, Stderr(Text("Warning"))).show(out, as_json=True)
    assert out.getvalue() == '{\n  "id": 1\n}\n'
    assert capsys.readouterr().err == ""


def test_verbatim_writes_its_text_as_it_is():
    out = io.StringIO()
    Views(Verbatim("a: 1\nb: 2"), Text("end")).write(out)
    assert out.getvalue() == "a: 1\nb: 2end\n"


def test_a_bare_table_prints_its_header_without_rows():
    assert Table(("PLAN", "REV"), [], bare=True).lines() == ["PLAN  REV"]
    assert Table(("PLAN", "REV"), [], "none").lines() == ["none"]
    assert Table(("PLAN", "REV"), [("a", 10)]).lines() == ["PLAN  REV", "a     10"]
