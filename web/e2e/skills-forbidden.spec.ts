import { call } from "../src/lib/api/client";

import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, type HubAdmin, newAccount, uniqueName } from "./support/hub";
import { apiOf } from "./support/memories";
import { packSkill, publishSkill } from "./support/skills";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

/**
 * Skills the visitor may not read, and failures. A project's skills are its members': anyone else gets 403 from the
 * API (the same whether the project exists or not) and the no-access state on the page, and asking for a bundle URL
 * is refused before any URL is signed. A shared skill for hub admins alone is, to anyone else, a skill that does not
 * exist: left out of the list, not found on its page and from the API. A skill that does not exist is not found; a
 * blob store outage at download time says so and starts nothing.
 */
test.skip(isDeployed, "publishes skills through the local stack");

async function projectSkill(admin: HubAdmin) {
  const project = uniqueName("skills-closed");
  await admin.registerProject(project);
  const writer = newAccount("writer");
  await admin.grant(project, writer.login, "writer", "internal");
  await publishSkill(await apiOf(writer), await packSkill("team-notes", "Closed."), "team-notes", project);
  return { project, writer };
}

test("a user without a grant gets the no-access state, and asking for the bundle URL answers 403", async ({
  page,
  admin,
  signInAs,
}) => {
  const { project } = await projectSkill(admin);
  await signInAs(newAccount("stranger"));

  for (const path of [
    `/v1/skills/projects/${project}/team-notes/bundle`,
    `/v1/skills/projects/${project}/team-notes/bundle?version=1`,
    `/v1/skills/projects/${project}/team-notes`,
    `/v1/skills?project=${project}`,
  ]) {
    const refused = await page.request.get(path);
    expect(refused.status(), path).toBe(403);
    const body = (await refused.json()) as Record<string, unknown>;
    expect(body.error).toBe("forbidden");
    expect(body).not.toHaveProperty("url");
  }
  // A project that does not exist answers the same.
  expect((await page.request.get(`/v1/skills/projects/${uniqueName("nope")}/team-notes/bundle`)).status()).toBe(403);

  await page.goto(`/p/${project}/skills`);
  await expect(page.locator("#main").getByTestId("state-forbidden")).toContainText("Bạn không có quyền xem trang này");
  await expect(page.locator("#main").getByTestId("skills-table")).toHaveCount(0);
  await page.goto(`/p/${project}/skills/team-notes`);
  await expect(page.locator("#main").getByTestId("state-forbidden")).toBeVisible();
  await expect(page.locator('[data-testid^="download-v"]')).toHaveCount(0);
});

test("a member never sees a shared skill for hub admins, nor changes who sees one", async ({ page, signInAs }) => {
  const adminApi = await apiOf(ADMIN_ACCOUNT);
  const closed = uniqueName("release-keys");
  const open = uniqueName("house-style");
  await publishSkill(adminApi, await packSkill(closed, "Rotate the keys."), closed, null);
  await publishSkill(adminApi, await packSkill(open, "Write like the team."), open, null);
  const set = await call(
    adminApi.PUT("/v1/skills/global/{name}/audience", { params: { path: { name: closed } }, body: { audience: "admins" } }),
  );
  expect(set).toEqual({ name: closed, audience: "admins", changed: true });
  await signInAs(newAccount("member"));

  await page.goto("/skills");
  const table = page.locator("#main").getByTestId("skills-table");
  await expect(table.locator(`[data-skill-name="${open}"]`)).toBeVisible();
  await expect(table.locator(`[data-skill-name="${closed}"]`)).toHaveCount(0);
  await expect(table.getByTestId("skill-audience-admins")).toHaveCount(0);

  // Its page is the page of a skill that does not exist, and the open one offers the member no audience switch.
  await page.goto(`/skills/${closed}`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toContainText(`Không tìm thấy skill ${closed}`);
  await expect(page.locator('[data-testid^="download-v"]')).toHaveCount(0);
  await page.goto(`/skills/${open}`);
  await expect(page.locator("#main").getByTestId("skill-latest")).toBeVisible();
  await expect(page.locator("#main").getByTestId("skill-audience")).toHaveCount(0);

  // Word for word what a skill that does not exist answers.
  const nobody = uniqueName("no-such-skill");
  const missing = (await (await page.request.get(`/v1/skills/global/${nobody}`)).json()) as { message: string };
  for (const path of [
    `/v1/skills/global/${closed}`,
    `/v1/skills/global/${closed}/bundle`,
    `/v1/skills/global/${closed}/bundle?version=1`,
  ]) {
    const refused = await page.request.get(path);
    expect(refused.status(), path).toBe(404);
    const body = (await refused.json()) as Record<string, unknown>;
    expect(body.error).toBe("not_found");
    expect(body.message).toBe(missing.message.replace(nobody, closed));
    expect(body).not.toHaveProperty("url");
  }
  const listed = (await (await page.request.get("/v1/skills?scope=global")).json()) as { name: string }[];
  expect(listed.map((skill) => skill.name)).toContain(open);
  expect(listed.map((skill) => skill.name)).not.toContain(closed);

  // The write needs a hub admin, with the session's CSRF header or without it.
  const csrf = (await (await page.request.get("/v1/auth/web/csrf")).json()) as { csrf: string; header: string };
  for (const name of [closed, open]) {
    const refused = await page.request.put(`/v1/skills/global/${name}/audience`, {
      data: { audience: "everyone" },
      headers: { [csrf.header]: csrf.csrf },
    });
    expect(refused.status(), name).toBe(403);
    expect(((await refused.json()) as Record<string, unknown>).error).toBe("forbidden");
  }
  const still = await call(adminApi.GET("/v1/skills/global/{name}", { params: { path: { name: closed } } }));
  expect(still.audience).toBe("admins");
});

test("a skill that does not exist is not found", async ({ page, member }) => {
  const me = await member();
  const name = uniqueName("no-such-skill");
  await page.goto(`/skills/${name}`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toContainText(`Không tìm thấy skill ${name}`);
  await page.goto(`/p/${me.projects[0]}/skills/${name}`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toBeVisible();
});

test("a blob store outage at download time says so and starts no download", async ({ page, admin, signInAs }) => {
  const { project, writer } = await projectSkill(admin);
  await signInAs(writer);
  await page.goto(`/p/${project}/skills/team-notes`);
  await page.route("**/team-notes/bundle?**", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: "unavailable", message: "the blob store did not answer", request_id: "rid-e2e-503" }),
    }),
  );
  let downloads = 0;
  page.on("download", () => {
    downloads += 1;
  });
  const latest = page.locator("#main").getByTestId("skill-latest");
  await latest.getByTestId("download-v1").click();
  await expect(latest.getByTestId("download-status-v1")).toHaveText(
    "Kho blob của hub chưa sẵn sàng nên chưa tải được. Thử lại sau, hoặc báo quản trị viên hub.",
  );
  await expect(latest.getByTestId("download-v1")).toBeEnabled();
  expect(downloads).toBe(0);
});
