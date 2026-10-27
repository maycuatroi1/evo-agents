"""A self-managed project, as criteria a4 and a5 of the plan company-ops-on-hub name them: a hub admin turns the state on
for a project with the GitHub owners they trust (``hub admin project self-managed``), and the admins of that project then
install, upgrade and remove isolated plugins from those owners' releases, and give grants and invite links, themselves,
each audited in the project. The plugin belongs to the project alone; an owner nobody trusts, a signer outside the repo,
a member below admin and a project that is not self-managed are refused; a grant above the caller's own role or max level
is refused.

GitHub is ``tests.hub.test_market.MarketGitHub``, Sigstore ``FakeSigstore``, the blob store a moto bucket."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from evo_agents.hub import tables
from tests.hub import live, pg
from tests.hub.test_market import PROJECT, actions, grant, publisher_rows, started
from tests.plugins.market_samples import LUMEN_REPO, LUMEN_SIGNER, lumen_release

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

OTHER_PROJECT = "other"
LADDER = ["public", "internal", "secret"]
NOT_YOURS = "is above your own"


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    with started(hub_db, tmp_path, s3) as running:
        live.add_project(hub_db, PROJECT, LADDER)
        live.add_project(hub_db, OTHER_PROJECT, LADDER)
        yield running


def put(hub, login: str, path: str, **body):
    return hub.client.put(path, json=body, headers=hub.who[login])


def self_managed(hub, owners: list[str] | None = None, project: str = PROJECT):
    body = {"enabled": owners is not None, "trusted_owners": owners or []}
    return hub.client.put(f"/v1/admin/projects/{project}/self-managed", json=body, headers=hub.who[live.ADMIN])


def on(hub, project: str = PROJECT, owners: tuple[str, ...] = ("lumen-labs",)) -> None:
    """``project`` self-managed for ``owners``, alice its admin up to internal and bob a writer."""
    assert self_managed(hub, list(owners), project).status_code == 200
    for login, role in (("alice", "admin"), ("bob", "writer")):
        body = {"role": role, "max_level": "internal"}
        url = f"/v1/admin/projects/{project}/grants/{login}"
        assert hub.client.put(url, json=body, headers=hub.who[live.ADMIN]).status_code == 200


def install(hub, login: str = "alice", project: str = PROJECT, **body):
    body = {"repo": LUMEN_REPO, "tag": "v1.0.0", **body}
    return hub.client.post(f"/v1/projects/{project}/plugins/install-github", json=body, headers=hub.who[login])


def plugin_row(hub, name: str = "lumen-notes") -> dict | None:
    found = hub.client.get("/v1/plugins", headers=hub.who[live.ADMIN]).json()
    return next((item for item in found if item["name"] == name), None)


# The state


def test_self_managed_is_turned_on_and_off_by_a_hub_admin_alone(hub):
    grant(hub, "alice", "admin")
    url = f"/v1/admin/projects/{PROJECT}/self-managed"
    assert put(hub, "alice", url, enabled=True, trusted_owners=["lumen-labs"]).status_code == 403
    assert put(hub, live.ADMIN, url, enabled=True, trusted_owners=[]).status_code == 422
    assert put(hub, live.ADMIN, url, enabled=False, trusted_owners=["lumen-labs"]).status_code == 422
    on_ = self_managed(hub, ["Lumen-Labs", "northwind", "lumen-labs"])
    assert on_.status_code == 200, on_.text
    assert (on_.json()["self_managed"], on_.json()["trusted_owners"]) == (True, ["lumen-labs", "northwind"])
    assert on_.json()["levels"] == LADDER and [g["login"] for g in on_.json()["grants"]] == ["alice"]
    assert hub.client.get(f"/v1/admin/projects/{PROJECT}", headers=hub.who["alice"]).status_code == 403
    assert hub.client.get(f"/v1/admin/projects/{PROJECT}", headers=hub.who[live.ADMIN]).json() == on_.json()
    own = hub.client.get(f"/v1/projects/{PROJECT}/admin", headers=hub.who["alice"])
    assert own.status_code == 200 and own.json() == on_.json()
    assert hub.client.get(f"/v1/projects/{PROJECT}/admin", headers=hub.who["bob"]).status_code == 404
    off = self_managed(hub)
    assert off.json()["self_managed"] is False and off.json()["trusted_owners"] == []
    assert [a for a in actions(hub.db) if a.startswith("project.self_managed")] == [
        f"project.self_managed {PROJECT} self_managed=true owners=lumen-labs,northwind",
        f"project.self_managed {PROJECT} self_managed=false",
    ]


# Plugins


def test_the_admin_of_a_self_managed_project_installs_upgrades_and_removes_a_trusted_owners_plugin(hub):
    on(hub)
    done = install(hub)
    assert done.status_code == 200, done.text
    found = done.json()
    assert (found["plugin"]["name"], found["plugin"]["installed_version"]) == ("lumen-notes", "1.0.0")
    assert (found["plugin"]["project"], found["trusted_publisher"], found["upgraded"]) == (PROJECT, False, False)
    assert publisher_rows(hub.db) == [], "the hub keeps no trust of the signer"
    hub.github.releases[LUMEN_REPO].append(lumen_release(hub.tmp / "v2", "1.1.0"))
    up = install(hub, tag=None, signer=LUMEN_SIGNER)
    assert up.status_code == 200 and up.json()["upgraded"] is True
    assert plugin_row(hub)["installed_version"] == "1.1.0"
    gone = hub.client.post(f"/v1/projects/{PROJECT}/plugins/lumen-notes/remove", headers=hub.who["alice"])
    assert gone.status_code == 200 and gone.json()["installed_version"] is None
    assert (
        hub.client.post(f"/v1/projects/{PROJECT}/plugins/lumen-notes/remove", headers=hub.who["alice"]).status_code
        == 409
    )
    taken = [a for a in actions(hub.db) if a.startswith("plugin.") and not a.startswith("plugin.publish")]
    assert [a.split()[0] for a in taken] == ["plugin.install", "plugin.upgrade", "plugin.remove"]
    assert all("lumen-notes" in a for a in taken)
    a = tables.audit
    rows = live.sql(
        hub.db, select(a.c.action, a.c.project_id).where(a.c.action.in_(("plugin.install", "plugin.remove")))
    )
    project_id = live.sql(hub.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    assert rows and {project for _, project in rows} == {project_id}, "audited in the project"


def test_a_self_managed_project_refuses_what_is_not_its_to_install(hub):
    grant(hub, "alice", "admin")
    not_yet = install(hub)
    assert not_yet.status_code == 403 and "not self-managed" in not_yet.json()["message"]
    on(hub)
    assert install(hub, "bob").status_code == 403, "a writer"
    assert install(hub, live.ADMIN).status_code == 403, "a hub admin with no grant on the project is not its admin"
    stranger = install(hub, repo="northwind/lumen-notes")
    assert stranger.status_code == 403 and "northwind is not a trusted owner" in stranger.json()["message"]
    forged = install(hub, signer="sigstore:northwind/fork/.github/workflows/ci.yml")
    assert forged.status_code == 422 and "workflow of the repository itself" in forged.json()["message"]
    assert install(hub, trust_publisher=True).status_code == 422, "no trust to keep: the field is not there"
    assert plugin_row(hub) is None and publisher_rows(hub.db) == []
    off = self_managed(hub)
    assert off.status_code == 200
    assert install(hub).status_code == 403, "off again"


def test_a_plugin_a_self_managed_project_installed_is_its_own_alone(hub):
    on(hub)
    on(hub, OTHER_PROJECT)
    assert install(hub).status_code == 200
    mine = hub.client.get(f"/v1/projects/{PROJECT}/plugins", headers=hub.who["alice"]).json()
    theirs = hub.client.get(f"/v1/projects/{OTHER_PROJECT}/plugins", headers=hub.who["alice"]).json()
    assert [p["name"] for p in mine] == ["lumen-notes"] and theirs == []
    asked = hub.client.get(f"/v1/projects/{PROJECT}/plugins/lumen-notes", headers=hub.who["alice"]).json()
    enable = {"permissions": [item["name"] for item in asked["permissions"]]}
    url = f"/v1/projects/{OTHER_PROJECT}/plugins/lumen-notes/enable"
    assert hub.client.post(url, json=enable, headers=hub.who["alice"]).status_code == 404
    assert (
        hub.client.post(url.replace(OTHER_PROJECT, PROJECT), json=enable, headers=hub.who["alice"]).status_code == 200
    )
    again = install(hub, project=OTHER_PROJECT)
    assert again.status_code == 409 and "another project's already" in again.json()["message"]
    removed = hub.client.post(f"/v1/projects/{OTHER_PROJECT}/plugins/lumen-notes/remove", headers=hub.who["alice"])
    assert removed.status_code == 404
    assert plugin_row(hub)["project"] == PROJECT and plugin_row(hub)["installed_version"] == "1.0.0"


# Grants and invites


def test_grant_and_invite_of_a_self_managed_project_stay_within_its_admins_own(hub):
    on(hub)
    base = f"/v1/projects/{PROJECT}/admin"
    # a hub admin gives dave an admin grant to the top of the ladder: above alice's internal
    top = {"role": "admin", "max_level": "secret"}
    assert (
        hub.client.put(f"/v1/admin/projects/{PROJECT}/grants/dave", json=top, headers=hub.who[live.ADMIN]).status_code
        == 200
    )
    given = put(hub, "alice", f"{base}/grants/carol", role="writer", max_level="internal")
    assert given.status_code == 200 and given.json()["created"] is True and given.json()["project"] == PROJECT
    changed = put(hub, "alice", f"{base}/grants/carol", role="reader", max_level="public")
    assert changed.status_code == 200 and changed.json()["created"] is False
    above_level = put(hub, "alice", f"{base}/grants/erin", role="reader", max_level="secret")
    assert above_level.status_code == 403 and NOT_YOURS in above_level.json()["message"]
    assert put(hub, "alice", f"{base}/grants/dave", role="reader", max_level="public").status_code == 403
    assert hub.client.delete(f"{base}/grants/dave", headers=hub.who["alice"]).status_code == 403
    assert put(hub, "bob", f"{base}/grants/carol", role="reader", max_level="public").status_code == 403
    assert hub.client.delete(f"{base}/grants/carol", headers=hub.who["alice"]).status_code == 204
    assert hub.client.delete(f"{base}/grants/carol", headers=hub.who["alice"]).status_code == 404
    taken = [a for a in actions(hub.db) if a.startswith(("grant.put", "grant.delete")) and "carol" in a]
    assert [a.split()[0] for a in taken] == ["grant.put", "grant.put", "grant.delete"]

    invited = hub.client.post(
        f"{base}/invites", json={"role": "writer", "max_level": "internal"}, headers=hub.who["alice"]
    )
    assert invited.status_code == 201, invited.text
    assert (invited.json()["project"], invited.json()["role"], invited.json()["max_level"]) == (
        PROJECT,
        "writer",
        "internal",
    )
    assert invited.json()["token"] and invited.headers["cache-control"] == "no-store"
    too_high = hub.client.post(
        f"{base}/invites", json={"role": "reader", "max_level": "secret"}, headers=hub.who["alice"]
    )
    assert too_high.status_code == 403 and NOT_YOURS in too_high.json()["message"]
    assert (
        hub.client.post(
            f"{base}/invites", json={"role": "reader", "max_level": "public"}, headers=hub.who["bob"]
        ).status_code
        == 403
    )
    assert [a.split()[0] for a in actions(hub.db) if a.startswith("invite.")] == ["invite.create"]


def test_grant_and_invite_of_a_project_that_is_not_self_managed_stay_a_hub_admins(hub):
    grant(hub, "alice", "admin")
    base = f"/v1/projects/{PROJECT}/admin"
    refused = put(hub, "alice", f"{base}/grants/carol", role="reader", max_level="public")
    assert refused.status_code == 403 and "not self-managed" in refused.json()["message"]
    assert hub.client.delete(f"{base}/grants/alice", headers=hub.who["alice"]).status_code == 403
    invite = hub.client.post(
        f"{base}/invites", json={"role": "reader", "max_level": "public"}, headers=hub.who["alice"]
    )
    assert invite.status_code == 403 and "not self-managed" in invite.json()["message"]
    assert hub.client.get(f"/v1/projects/{PROJECT}/admin", headers=hub.who["alice"]).status_code == 200
