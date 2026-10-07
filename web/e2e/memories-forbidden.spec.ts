import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import { apiOf, memoryFile, memoryProject, putMemory } from "./support/memories";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

/**
 * Memories the visitor may not read, and failures: the API decides, the page shows its answer. Without a grant the
 * project is not found (the API answers 404, as for a project that does not exist); a hub admin without a grant is
 * told why nothing shows; a 5xx shows the error state with its request id and a retry.
 */
test.skip(isDeployed, "seeds users through the local stack");

test("a user without a grant finds neither the project's memories nor any one of them", async ({ page, admin, signInAs }) => {
  const project = uniqueName("mem-closed");
  await admin.registerProject(project, memoryProject());
  const author = newAccount("author");
  await admin.grant(project, author.login, "writer", "secret");
  const memory = await putMemory(await apiOf(author), {
    project,
    name: "plan.md",
    body: memoryFile("Plan", "Closed", "not for strangers"),
  });
  await signInAs(newAccount("stranger"));

  expect((await page.request.get(`/v1/memories?project=${project}`)).status()).toBe(404);
  expect((await page.request.get(`/v1/memories/search?project=${project}&q=plan`)).status()).toBe(404);
  expect((await page.request.get(`/v1/memories/${memory.id}`)).status()).toBe(404);

  await page.goto(`/p/${project}/memories`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toContainText(`Không tìm thấy dự án ${project}`);
  await page.goto(`/p/${project}/memories/${memory.id}`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toContainText("Không tìm thấy memory");
  await expect(page.locator("#main")).not.toContainText("not for strangers");
});

test("a hub admin without a grant reads no memory and is told why", async ({ page, admin, signInAs }) => {
  const project = uniqueName("mem-admin");
  await admin.registerProject(project, memoryProject());
  const author = newAccount("author");
  await admin.grant(project, author.login, "writer", "secret");
  await putMemory(await apiOf(author), { project, name: "plan.md", body: memoryFile("Plan", "Team", "team only") });
  await signInAs(ADMIN_ACCOUNT);

  const listed = await page.request.get(`/v1/memories?project=${project}`);
  expect(listed.status()).toBe(200);
  expect((await listed.json()).items).toEqual([]);
  await page.goto(`/p/${project}/memories`);
  await expect(page.locator("#main").getByRole("note")).toContainText("chưa có grant trên dự án này");
  await expect(page.locator("#main").getByTestId("memories-table")).toHaveCount(0);
});

test("a server error while searching shows the error state with its request id, and a retry recovers", async ({
  page,
  admin,
  signInAs,
}) => {
  // One memory the reader may read, so the list and its search field show: a project with none shows only its
  // first-use empty state, without a toolbar over nothing.
  const project = uniqueName("mem-error");
  await admin.registerProject(project, memoryProject());
  const author = newAccount("author");
  await admin.grant(project, author.login, "writer", "internal");
  await putMemory(await apiOf(author), { project, name: "plan.md", body: memoryFile("Plan", "Team", "team only") });
  const reader = newAccount("reader");
  await admin.grant(project, reader.login, "reader", "internal");
  await signInAs(reader);
  await page.goto(`/p/${project}/memories`);
  await expect(page.locator("#main").getByTestId("memories-table")).toBeVisible();
  await page.route("**/v1/memories/search?**", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      headers: { "x-request-id": "rid-e2e-search" },
      body: JSON.stringify({ error: "internal_error", message: "boom", request_id: "rid-e2e-search" }),
    }),
  );
  await page.locator("#main").getByTestId("memories-search").fill("anything");
  const failed = page.locator("#main").getByTestId("state-error");
  await expect(failed).toBeVisible({ timeout: 20_000 }); // a 5xx is retried twice first
  await expect(failed).toContainText("Hub gặp lỗi khi xử lý yêu cầu");
  await expect(failed).toContainText("rid-e2e-search");
  await page.unroute("**/v1/memories/search?**");
  await failed.getByRole("button", { name: "Thử lại" }).click();
  await expect(page.locator("#main").getByTestId("state-error")).toHaveCount(0);
  await expect(page.locator("#main").getByTestId("state-empty")).toContainText("Không tìm thấy memory nào cho “anything”");
});
