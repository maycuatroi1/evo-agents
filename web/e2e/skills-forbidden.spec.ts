import { expect, isDeployed, test } from "./support/fixtures";
import { type HubAdmin, newAccount, uniqueName } from "./support/hub";
import { apiOf } from "./support/memories";
import { packSkill, publishSkill } from "./support/skills";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

/**
 * Skills the visitor may not read, and failures. A project's skills are its members': anyone else gets 403 from the
 * API (the same whether the project exists or not) and the no-access state on the page, and asking for a bundle URL
 * is refused before any URL is signed. A skill that does not exist is not found; a blob store outage at download
 * time says so and starts nothing.
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
