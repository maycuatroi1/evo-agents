"""The rules of the outputs of runs both sides share (``evo_agents.hub.outputs``): names, media types, how the hub serves
each type (HTML and SVG never with the hub's origin), the names a download is offered under, and the byte ranges of a
Range header. Standard library only, no Postgres."""

from __future__ import annotations

import pytest

from evo_agents.hub import outputs


@pytest.mark.parametrize(
    ("header", "size", "span"),
    [
        (None, 10, None),
        ("bytes=0-3", 10, (0, 3)),
        ("bytes=4-", 10, (4, 9)),
        ("bytes=-3", 10, (7, 9)),
        ("bytes=-30", 10, (0, 9)),
        ("bytes=8-100", 10, (8, 9)),
        ("bytes=10-", 10, False),
        ("bytes=5-4", 10, False),
        ("bytes=-0", 10, False),
        ("bytes=0-", 0, False),
        ("bytes=0-1,4-5", 10, None),
        ("items=0-1", 10, None),
        ("bytes=a-b", 10, None),
        ("bytes=-", 10, None),
    ],
)
def test_output_ranges_read_one_span_and_say_when_it_lies_outside(header, size, span):
    assert outputs.parse_range(header, size) == span


@pytest.mark.parametrize("name", ["report.md", "screens/login.png", "Báo cáo tuần.md", "a" * 255])
def test_an_output_name_is_a_file_or_a_relative_path(name):
    assert outputs.check_name(f"  {name} ") == name


@pytest.mark.parametrize("name", ["", " ", "/etc/passwd", "../up.md", "a/./b", "a//b", "a\\b", "a\nb", "a" * 256])
def test_an_output_name_that_leaves_its_folder_or_holds_a_control_is_refused(name):
    with pytest.raises(ValueError):
        outputs.check_name(name)


def test_output_media_types_are_one_type_and_subtype_in_lower_case():
    assert outputs.check_media_type(" Image/PNG ") == "image/png"
    for bad in ("text/html; charset=utf-8", "png", "image/", "/png", "image/png/x"):
        with pytest.raises(ValueError):
            outputs.check_media_type(bad)
    assert outputs.guess_media_type("demo.mp4") == "video/mp4"
    assert outputs.guess_media_type("notes/report.md") == "text/markdown"
    assert outputs.guess_media_type("app.ts") == "text/plain"
    assert outputs.guess_media_type("bundle.tar.gz") == "application/octet-stream"
    assert outputs.guess_media_type("no-extension") == "application/octet-stream"


@pytest.mark.parametrize(
    ("media_type", "viewer", "served", "policy"),
    [
        ("image/png", "image", "image/png", outputs.OPAQUE_POLICY),
        ("image/svg+xml", "image", "image/svg+xml", outputs.OPAQUE_POLICY),
        ("video/mp4", "video", "video/mp4", outputs.OPAQUE_POLICY),
        ("application/pdf", "pdf", "application/pdf", outputs.PDF_POLICY),
        ("text/html", "html", "text/html; charset=utf-8", outputs.HTML_POLICY),
        ("application/xhtml+xml", "html", "text/html; charset=utf-8", outputs.HTML_POLICY),
        ("text/markdown", "text", "text/plain; charset=utf-8", outputs.OPAQUE_POLICY),
        ("application/xml", "text", "text/plain; charset=utf-8", outputs.OPAQUE_POLICY),
        ("application/json", "text", "text/plain; charset=utf-8", outputs.OPAQUE_POLICY),
        ("application/zip", "none", "application/zip", outputs.OPAQUE_POLICY),
    ],
)
def test_each_output_type_has_its_viewer_its_served_type_and_its_policy(media_type, viewer, served, policy):
    assert (outputs.viewer(media_type), outputs.served_type(media_type)) == (viewer, served)
    assert outputs.content_policy(media_type) == policy
    assert outputs.inline(media_type) is (viewer != "none")


def test_html_and_svg_outputs_get_an_opaque_origin_that_calls_nothing():
    for policy in (outputs.HTML_POLICY, outputs.OPAQUE_POLICY):
        assert "sandbox" in policy and "allow-same-origin" not in policy
        assert "frame-ancestors 'self'" in policy
    assert "connect-src 'none'" in outputs.HTML_POLICY and "form-action 'none'" in outputs.HTML_POLICY
    assert "script-src" not in outputs.OPAQUE_POLICY
    assert outputs.is_text("text/html") and outputs.is_text("image/svg+xml") and not outputs.is_text("image/png")


def test_an_output_is_offered_for_download_under_a_safe_ascii_name_and_its_own():
    assert outputs.download_name("screens/login.png") == "login.png"
    assert outputs.download_name("Báo cáo.md") == "B_o_c_o.md"
    assert outputs.download_name('a"b;c.txt') == "a_b_c.txt"
    assert outputs.download_name("...") == "output"
    long = outputs.download_name("x" * 300 + ".markdown")
    assert len(long) == outputs.DOWNLOAD_NAME_CHARS and long.endswith(".markdown")
    assert outputs.content_disposition("shots/Ảnh.png", attachment=True) == (
        "attachment; filename=\"nh.png\"; filename*=UTF-8''%E1%BA%A2nh.png"
    )
    assert outputs.content_disposition("a.png", attachment=False).startswith('inline; filename="a.png"')
