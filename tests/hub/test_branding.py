"""The branding of the hub, as criterion a11 of plan hub-agent-plugins names it (``evo_agents.hub.branding``,
``evo_agents.hub.server.branding``): with nothing set, GET /v1/branding leaves the web as 0.16.1 had it, with no name,
no image and no colour; a hub admin sets the product's name and the colours of the four tokens of web/DESIGN.md a hub
may set, which must keep the contrast web/DESIGN.md asks for in both themes or are refused with nothing changed;
uploads a logo and a favicon to the blob store; and rolls back to an older version, each version an audit row. Anyone
else gets 403 on the admin routes, and the sign-in page reads the branding and its images without a credential."""

import base64
import hashlib
import secrets

import pytest

from evo_agents.hub import branding
from tests.hub import live, pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + secrets.token_bytes(64)
ICO = b"\x00\x00\x01\x00\x01\x00" + secrets.token_bytes(40)
COLORS = {"light": {"brand": "#0F766E", "brand-hover": "#115E59"}, "dark": {"brand": "#5EEAD4"}}
EMPTY = {"name": None, "logo_url": None, "favicon_url": None, "colors": {"light": {}, "dark": {}}, "version": 0}


# The rules, without a hub


def test_the_default_tokens_have_the_contrast_design_md_measures():
    ratios = {
        ("light", "brand", "surface"): 6.7,
        ("light", "brand", "brand-soft"): 5.8,
        ("light", "brand-hover", "surface"): 8.4,
        ("light", "on-brand", "brand"): 6.7,
        ("dark", "brand", "surface"): 7.4,
        ("dark", "brand", "brand-soft"): 6.2,
        ("dark", "brand-hover", "surface"): 9.0,
        ("dark", "on-brand", "brand"): 7.8,
    }
    for (theme, front, ground), ratio in ratios.items():
        tokens = branding.DEFAULTS[theme]
        assert round(branding.contrast(tokens[front], tokens[ground]), 1) == ratio, (theme, front, ground)
    assert branding.contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert branding.contrast("#777777", "#777777") == 1.0
    assert branding.contrast_problems(None) == [] and branding.contrast_problems({}) == []


def test_a_write_of_the_branding_is_checked_field_by_field_then_for_contrast():
    def problems(values: dict) -> list[str]:
        return branding.problems(values, values)

    assert problems({"name": "Acme Agents", "colors": COLORS}) == []
    assert problems({"theme": "dark"}) == [
        "theme: not a field of the branding, whose fields are name, colors, logo, favicon"
    ]
    assert problems({"logo": {"sha256": "a" * 64}}) == [
        "logo: an image, set by PUT /v1/admin/branding/logo with its bytes"
    ]
    for name in ("", "   ", 7, "x" * 61, " Acme", "Acme\nAgents"):
        assert problems({"name": name})[0].startswith("name: "), name
    assert problems({"colors": {"sepia": {}}}) == ["colors.sepia: not a theme; the themes are light, dark"]
    assert problems({"colors": {"light": {"danger": "#b3261e"}}})[0].startswith("colors.light.danger: not a token")
    for value in ("#fff", "0f766e", "#0f766g", "teal", 3):
        assert problems({"colors": {"light": {"brand": value}}}) == ["colors.light.brand: a colour as #rrggbb"]

    pale = problems({"colors": {"light": {"brand": "#9aa5ff"}}})
    assert pale[0] == (
        "colors.light: brand #9aa5ff on surface #ffffff is 2.28:1, under the 4.5:1 web/DESIGN.md asks for links and "
        "the running pill on a card"
    )
    assert any("on-brand #ffffff on brand #9aa5ff" in problem for problem in pale)
    # a token left unset counts with its default: a dark on-brand on a dark brand fails, whatever brand-soft does
    assert problems({"colors": {"dark": {"brand": "#1e2a78"}}})[0].startswith("colors.dark: brand #1e2a78 on surface")
    assert branding.normalized({"colors": COLORS})["colors"]["light"]["brand"] == "#0f766e"


# The routes

if pg.DSN:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from evo_agents.hub import tables
    from evo_agents.hub.server.app import create_app
    from tests.hub.test_runs import audit_rows, members


@pytest.fixture
def hub(hub_db, tmp_path, github, s3):
    """test_runs' members on a hub with a blob store: the headers of admin (the hub's), owner, other, reader and
    stranger, and the client."""
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client, members(client, github)


def write(client, headers, status: int = 200, **body):
    response = client.post("/v1/admin/branding", json=body, headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def image(data: bytes, **body) -> dict:
    return {"data": base64.b64encode(data).decode(), **body}


@needs_pg
def test_without_branding_the_hub_answers_what_leaves_the_web_as_it_was(hub):
    client, headers = hub
    assert client.get("/v1/branding").json() == EMPTY  # without a credential: the sign-in page reads it
    for asset in ("logo", "favicon"):
        missing = client.get(f"/v1/branding/{asset}")
        assert missing.status_code == 404 and missing.json()["error"] == "not_found"
    layer = client.get("/v1/admin/branding", headers=headers["admin"]).json()
    assert (layer["kind"], layer["project"], layer["version"], layer["values"]) == ("branding", None, 0, {})
    assert client.get("/v1/admin/branding/versions", headers=headers["admin"]).json() == []


@needs_pg
def test_a_hub_admin_sets_the_name_and_colours_and_a_colour_under_its_contrast_changes_nothing(hub, hub_db):
    client, headers = hub
    layer = write(client, headers["admin"], values={"name": "Acme Agents", "colors": COLORS}, if_version=0)
    assert layer["version"] == 1 and layer["changed_by"] == live.ADMIN
    lowered = {"light": {"brand": "#0f766e", "brand-hover": "#115e59"}, "dark": {"brand": "#5eead4"}}
    assert layer["values"] == {"name": "Acme Agents", "colors": lowered}
    assert client.get("/v1/branding").json() == {**EMPTY, "name": "Acme Agents", "colors": lowered, "version": 1}

    refused = write(client, headers["admin"], 422, values={"colors": {"light": {"brand": "#9aa5ff"}}})
    assert "brand #9aa5ff on surface #ffffff is 2.28:1" in refused["message"]
    assert refused["message"].endswith("nothing changed")
    stale = write(client, headers["admin"], 409, values={"name": "Other"}, if_version=0)
    assert stale["message"] == "the hub's branding is at version 1, not 0; nothing changed"
    assert write(client, headers["admin"], values={"name": "Acme Agents"})["version"] == 1  # nothing to change
    assert write(client, headers["admin"], values={}, unset=["colors"])["values"] == {"name": "Acme Agents"}
    assert write(client, headers["admin"], 422, unset=["logo"])["message"].startswith("logo: the layer does not set")

    for who in ("owner", "reader", "stranger"):
        assert client.get("/v1/admin/branding", headers=headers[who]).status_code == 403
        assert (
            client.post("/v1/admin/branding", json={"values": {"name": "Mine"}}, headers=headers[who]).status_code
            == 403
        )
    assert client.post("/v1/admin/branding", json={"values": {"name": "Mine"}}).status_code == 401
    assert audit_rows(hub_db, "settings") == [
        ("settings.branding", "branding v1 fields=colors,name", live.ADMIN, None),
        ("settings.branding", "branding v2 fields=colors", live.ADMIN, None),
    ]


@needs_pg
def test_a_logo_and_a_favicon_go_to_the_blob_store_and_the_sign_in_page_reads_them(hub, hub_db, s3):
    client, headers = hub
    admin = headers["admin"]
    logo = client.put("/v1/admin/branding/logo", json=image(PNG), headers=admin)
    assert logo.status_code == 200, logo.text
    sha = hashlib.sha256(PNG).hexdigest()
    assert logo.json()["values"] == {"logo": {"sha256": sha, "type": "image/png", "size": len(PNG)}}
    favicon = client.put("/v1/admin/branding/favicon", json=image(ICO, if_version=1), headers=admin)
    assert favicon.status_code == 200 and favicon.json()["version"] == 2

    shown = client.get("/v1/branding").json()
    assert shown["logo_url"] == f"/v1/branding/logo?v={sha[:16]}"
    assert shown["favicon_url"] == f"/v1/branding/favicon?v={hashlib.sha256(ICO).hexdigest()[:16]}"
    served = client.get(shown["logo_url"])  # no credential
    assert served.status_code == 200 and served.content == PNG
    assert served.headers["content-type"] == "image/png" and served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["cache-control"] == "public, max-age=86400"
    assert served.headers["content-security-policy"] == "default-src 'none'"
    icon = client.get(shown["favicon_url"])
    assert icon.content == ICO and icon.headers["content-type"] == "image/x-icon"
    assert s3.get(f"blobs/sha256/{sha}") == PNG
    b = tables.blobs
    held = live.sql(hub_db, select(b.c.project_id, b.c.kind, b.c.size).where(b.c.sha256 == sha))
    assert held == [(None, "branding-image", len(PNG))]

    svg = client.put("/v1/admin/branding/logo", json=image(b"<svg xmlns='http://www.w3.org/2000/svg'/>"), headers=admin)
    assert svg.status_code == 422 and "PNG, JPEG or WebP" in svg.json()["message"]
    ico_logo = client.put("/v1/admin/branding/logo", json=image(ICO), headers=admin)
    assert ico_logo.status_code == 422
    big = client.put("/v1/admin/branding/logo", json=image(b"\x89PNG\r\n\x1a\n" + bytes(256 * 1024)), headers=admin)
    assert big.status_code in (413, 422), big.text
    assert client.put("/v1/admin/branding/logo", json=image(PNG), headers=headers["owner"]).status_code == 403
    assert client.put("/v1/admin/branding/banner", json=image(PNG), headers=admin).status_code == 422

    dropped = write(client, admin, values={}, unset=["logo"])
    assert dropped["version"] == 3 and "logo" not in dropped["values"]
    assert client.get("/v1/branding").json()["logo_url"] is None
    assert client.get("/v1/branding/logo").status_code == 404


@needs_pg
def test_rolling_the_branding_back_copies_an_older_version_as_the_next(hub, hub_db):
    client, headers = hub
    admin = headers["admin"]
    write(client, admin, values={"name": "Acme", "colors": COLORS})
    write(client, admin, values={"name": "Acme Agents"}, unset=["colors"])
    back = client.post("/v1/admin/branding/rollback", json={"version": 1, "if_version": 2}, headers=admin)
    assert back.status_code == 200, back.text
    assert (back.json()["version"], back.json()["rolled_back_to"], back.json()["values"]["name"]) == (3, 1, "Acme")
    assert client.get("/v1/branding").json()["colors"]["dark"] == {"brand": "#5eead4"}
    versions = client.get("/v1/admin/branding/versions", headers=admin).json()
    assert [(v["version"], v["rolled_back_to"]) for v in versions] == [(3, 1), (2, None), (1, None)]
    for body, status in (({"version": 3}, 409), ({"version": 1, "if_version": 1}, 409), ({"version": 0}, 422)):
        assert client.post("/v1/admin/branding/rollback", json=body, headers=admin).status_code == status, body
    assert audit_rows(hub_db, "settings")[-1] == ("settings.branding", "branding v3 rollback-to=v1", live.ADMIN, None)
