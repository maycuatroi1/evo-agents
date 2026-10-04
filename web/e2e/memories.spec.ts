import type { Locator, Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { type Account, type HubAdmin, newAccount, uniqueName } from "./support/hub";
import { apiOf, type Memory, memoryFile, memoryProject, putMemory } from "./support/memories";

/**
 * Memories on the web, against the real API: what a member browses, searches and reads is what the API returns for
 * the browser's own session, filtered by the grant's max level and by owner on the server. Each test seeds its own
 * project through the API as a writer whose grant reaches secret, and signs in as a reader whose grant reaches
 * internal.
 *
 * Locators stay inside #main: while a page streams in, React keeps a hidden copy of a segment outside it for a
 * moment, which a page-wide test id would also match.
 */
test.skip(isDeployed, "seeds memories through the local stack");

async function seededProject(admin: HubAdmin) {
  const project = uniqueName("mem");
  await admin.registerProject(project, memoryProject());
  const author = newAccount("author");
  const reader = newAccount("reader");
  await admin.grant(project, author.login, "writer", "secret");
  await admin.grant(project, reader.login, "reader", "internal");
  return { project, author, reader, authorApi: await apiOf(author) };
}

function memoryNames(table: Locator): Promise<string[]> {
  return table
    .locator("[data-memory-name]")
    .evaluateAll((links) => links.map((link) => link.getAttribute("data-memory-name") ?? "").sort());
}

async function apiNames(page: Page, path: string): Promise<string[]> {
  const response = await page.request.get(path);
  expect(response.status(), path).toBe(200);
  return ((await response.json()) as { items: Memory[] }).items.map((memory) => memory.name).sort();
}

test("a member browses a project's memories by location and type, searches them and reads one", async ({
  page,
  admin,
  signInAs,
}) => {
  const { project, reader, authorApi } = await seededProject(admin);
  const deploy = await putMemory(authorApi, {
    project,
    name: "deploy.md",
    body: memoryFile("Deploy notes", "How we ship", "Run **make release** after the wombat check."),
  });
  await putMemory(authorApi, {
    project,
    location: "api",
    name: "api-conventions.md",
    type: "reference",
    level: "public",
    body: memoryFile("API conventions", "Errors and paging", "Every list pages by cursor.", "reference"),
  });
  await putMemory(authorApi, {
    project,
    location: "web",
    name: "web-notes.md",
    body: memoryFile("Web notes", "The web", "The web ships with the API."),
  });
  await signInAs(reader);

  await page.goto(`/p/${project}/memories`);
  const table = page.locator("#main").getByTestId("memories-table");
  await expect(table).toBeVisible();
  expect(await memoryNames(table)).toEqual(["api-conventions.md", "deploy.md", "web-notes.md"]);
  await expect(page.locator("#main").getByTestId("memories-summary")).toHaveText("3 memory");

  // Facets narrow the list and live in the URL.
  const locations = page.locator("#main").getByTestId("facet-location");
  await locations.locator('[data-facet-value="api"]').click();
  await expect(page).toHaveURL(/[?&]location=api/);
  await expect(locations.locator('[data-facet-value="api"]')).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => memoryNames(table)).toEqual(["api-conventions.md"]);
  await expect(page.locator("#main").getByTestId("memories-summary")).toHaveText("Hiện 1 trên 3 memory");
  await locations.locator('[data-facet-value=""]').click();
  await page.locator("#main").getByTestId("facet-type").locator('[data-facet-value="reference"]').click();
  await expect(page).toHaveURL(/[?&]type=reference/);
  await expect.poll(() => memoryNames(table)).toEqual(["api-conventions.md"]);
  await page.locator("#main").getByRole("button", { name: "Xoá bộ lọc" }).click();
  await expect.poll(() => memoryNames(table)).toEqual(["api-conventions.md", "deploy.md", "web-notes.md"]);

  // Full-text search, ranked by the API.
  await page.locator("#main").getByTestId("memories-search").fill("wombat");
  await expect(page.locator("#main").getByTestId("memories-summary")).toHaveText("1 kết quả cho “wombat”");
  await expect(page).toHaveURL(/[?&]q=wombat/);
  expect(await memoryNames(table)).toEqual(["deploy.md"]);
  await page.reload(); // the search is in the URL, so a reload keeps it
  await expect(page.locator("#main").getByTestId("memories-summary")).toHaveText("1 kết quả cho “wombat”");

  // One memory, rendered from Markdown, and as written.
  await table.getByRole("link", { name: "Deploy notes" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/memories/${deploy.id}$`));
  await expect(page.locator("#main").getByRole("heading", { level: 1, name: "Deploy notes" })).toBeVisible();
  await expect(page.locator("#main").getByTestId("memory-markdown").locator("strong")).toHaveText("make release");
  await expect(page.locator("#main").getByTestId("memory-details")).toContainText("deploy.md");
  await expect(page.locator('[data-label-level="internal"]').first()).toBeVisible();
  await page.locator("#main").getByTestId("view-raw").click();
  await expect(page.locator("#main").getByTestId("memory-raw")).toContainText("Run **make release** after the wombat check.");
});

test("a reader whose grant reaches internal never sees a customer memory, even searching its exact name", async ({
  page,
  admin,
  signInAs,
}) => {
  const { project, reader, authorApi } = await seededProject(admin);
  const customer = await putMemory(authorApi, {
    project,
    name: "customer-acme.md",
    level: "customer",
    body: memoryFile("Customer ACME", "Contract terms", "ACME pays net 30. platypus"),
  });
  await putMemory(authorApi, {
    project,
    name: "internal-notes.md",
    body: memoryFile("Internal notes", "Notes", "platypus internal"),
  });
  await signInAs(reader);

  // The API answers the browser's own session by the grant: nothing above internal, by list, search or id.
  expect(await apiNames(page, `/v1/memories?project=${project}`)).toEqual(["internal-notes.md"]);
  for (const q of ["customer-acme.md", "customer-acme", "ACME", "platypus"]) {
    const names = await apiNames(page, `/v1/memories/search?project=${project}&q=${encodeURIComponent(q)}`);
    expect(names, q).not.toContain("customer-acme.md");
  }
  expect((await page.request.get(`/v1/memories/${customer.id}`)).status()).toBe(404);
  expect((await page.request.get(`/v1/memories/${customer.id}/revisions`)).status()).toBe(404);

  await page.goto(`/p/${project}/memories`);
  const table = page.locator("#main").getByTestId("memories-table");
  await expect(table).toBeVisible();
  expect(await memoryNames(table)).toEqual(["internal-notes.md"]);
  await page.locator("#main").getByTestId("memories-search").fill("customer-acme.md");
  await expect(page.locator("#main").getByTestId("state-empty")).toContainText("Không tìm thấy memory nào cho “customer-acme.md”");
  await page.locator("#main").getByTestId("memories-search").fill("ACME");
  await expect(page.locator("#main").getByTestId("state-empty")).toContainText("Không tìm thấy memory nào cho “ACME”");

  await page.goto(`/p/${project}/memories/${customer.id}`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toContainText("Không tìm thấy memory");
  await expect(page.locator("#main")).not.toContainText("net 30");
});

test("another member's user and feedback memories are not listed, and the API answers 404 as for a missing one", async ({
  page,
  admin,
  signInAs,
}) => {
  const { project, reader, authorApi } = await seededProject(admin);
  const own = [
    await putMemory(authorApi, {
      project,
      name: "author-prefs.md",
      type: "user",
      body: memoryFile("Author prefs", "Mine", "only mine", "user"),
    }),
    await putMemory(authorApi, {
      project,
      name: "author-feedback.md",
      type: "feedback",
      body: memoryFile("Author feedback", "Mine", "only mine too", "feedback"),
    }),
  ];
  await putMemory(authorApi, { project, name: "shared.md", body: memoryFile("Shared", "For everyone", "shared") });
  await signInAs(reader);

  const missingId = 999_999_999_999;
  const missing = await page.request.get(`/v1/memories/${missingId}`);
  expect(missing.status()).toBe(404);
  const missingBody = (await missing.json()) as { error: string; message: string };
  for (const memory of own) {
    const hidden = await page.request.get(`/v1/memories/${memory.id}`);
    expect(hidden.status()).toBe(404);
    const body = (await hidden.json()) as { error: string; message: string };
    expect(body.error).toBe(missingBody.error);
    expect(body.message.replace(String(memory.id), "N")).toBe(missingBody.message.replace(String(missingId), "N"));
    expect(JSON.stringify(body)).not.toContain(memory.name);
    expect((await page.request.get(`/v1/memories/${memory.id}/revisions/1`)).status()).toBe(404);
  }
  // Its author still reads it.
  const mine = await authorApi.GET("/v1/memories/{memory_id}", { params: { path: { memory_id: own[0].id } } });
  expect(mine.response.status).toBe(200);

  await page.goto(`/p/${project}/memories`);
  const table = page.locator("#main").getByTestId("memories-table");
  await expect(table).toBeVisible();
  expect(await memoryNames(table)).toEqual(["shared.md"]);
  const types = page.locator("#main").getByTestId("facet-type");
  await expect(types.locator('[data-facet-value="user"]')).toContainText("0");
  await expect(types.locator('[data-facet-value="feedback"]')).toContainText("0");
  await page.locator("#main").getByTestId("memories-search").fill("only mine");
  await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();

  await page.goto(`/p/${project}/memories/${own[0].id}`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toContainText("Không tìm thấy memory");
});

test("markdown holding a script tag shows it as text and never runs it", async ({ page, admin, signInAs }) => {
  const { project, reader, authorApi } = await seededProject(admin);
  const text = [
    "Before the tags.",
    "<script>window.__pwned = 'script'</script>",
    "<img src=\"x\" onerror=\"window.__pwned = 'img'\">",
    "[click me](javascript:window.__pwned='link')",
    "<a href=\"javascript:window.__pwned='raw'\">raw link</a>",
  ].join("\n\n");
  const memory = await putMemory(authorApi, { project, name: "evil.md", body: memoryFile("Evil", "Tries to run", text) });
  await signInAs(reader);
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => {
    dialogs.push(dialog.message());
    void dialog.dismiss();
  });

  await page.goto(`/p/${project}/memories/${memory.id}`);
  const markdown = page.locator("#main").getByTestId("memory-markdown");
  await expect(markdown).toContainText("<script>window.__pwned = 'script'</script>");
  await expect(markdown).toContainText("<img src=\"x\" onerror=\"window.__pwned = 'img'\">");
  await expect(markdown.locator("script, img, iframe, [onerror], [onclick]")).toHaveCount(0);
  expect(await markdown.locator("a").count()).toBe(0); // the javascript: links are text
  await markdown.getByText("click me").click();
  await markdown.getByText("raw link", { exact: false }).click();
  expect(await page.evaluate(() => (window as Window & { __pwned?: unknown }).__pwned)).toBeUndefined();
  expect(dialogs).toEqual([]);
  await expect(page).toHaveURL(new RegExp(`/memories/${memory.id}$`));
});

test("the history lists the revisions the reader may see and opens an older one", async ({ page, admin, signInAs }) => {
  const { project, reader, authorApi } = await seededProject(admin);
  const first = await putMemory(authorApi, {
    project,
    name: "rollout.md",
    level: "customer",
    body: memoryFile("Rollout", "Plan", "Draft naming ACME"),
  });
  await putMemory(authorApi, { project, name: "rollout.md", ifRevision: 1, body: memoryFile("Rollout", "Plan", "Cleaned up") });
  await putMemory(authorApi, { project, name: "rollout.md", ifRevision: 2, body: memoryFile("Rollout", "Plan", "Final plan") });
  await signInAs(reader);

  await page.goto(`/p/${project}/memories/${first.id}`);
  const history = page.locator("#main").getByTestId("revision-history");
  await expect(history).toBeVisible();
  // Revision 1 carried a customer label: it is not in the reader's history at all.
  expect(await history.locator("li").evaluateAll((items) => items.map((item) => item.getAttribute("data-revision")))).toEqual(["3", "2"]);
  await expect(history.locator('[aria-current="page"]')).toContainText("Revision 3");
  await expect(page.locator("#main").getByTestId("memory-markdown")).toContainText("Final plan");
  await expect(page.locator("#main")).not.toContainText("ACME");

  await history.getByRole("link", { name: /Revision 2/ }).click();
  await expect(page).toHaveURL(new RegExp(`/memories/${first.id}\\?revision=2$`));
  await expect(page.locator("#main").getByTestId("old-revision")).toContainText("Đang xem revision 2, bản mới nhất là revision 3");
  await expect(page.locator("#main").getByTestId("memory-markdown")).toContainText("Cleaned up");
  await expect(history.locator('[aria-current="page"]')).toContainText("Revision 2");
  await page.locator("#main").getByRole("link", { name: "Xem bản mới nhất (revision 3)" }).click();
  await expect(page).toHaveURL(new RegExp(`/memories/${first.id}$`));
  await expect(page.locator("#main").getByTestId("memory-markdown")).toContainText("Final plan");

  expect((await page.request.get(`/v1/memories/${first.id}/revisions/1`)).status()).toBe(404);
  await page.goto(`/p/${project}/memories/${first.id}?revision=1`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toContainText("Không tìm thấy revision 1");
  await expect(page.locator("#main")).not.toContainText("ACME");
});

test("personal memories are their owner's alone", async ({ page, signInAs }) => {
  const owner: Account = newAccount("owner");
  const ownerApi = await apiOf(owner);
  const mine = await putMemory(ownerApi, {
    name: "reading-list.md",
    body: memoryFile("Reading list", "Papers to read", "- Harness engineering"),
  });
  await signInAs(owner);
  await page.goto("/memories");
  const table = page.locator("#main").getByTestId("memories-table");
  await expect(table).toBeVisible();
  expect(await memoryNames(table)).toEqual(["reading-list.md"]);
  await table.getByRole("link", { name: "Reading list" }).click();
  await expect(page).toHaveURL(new RegExp(`/memories/${mine.id}$`));
  await expect(page.locator("#main").getByTestId("memory-details")).toContainText("Cá nhân");

  const other = await apiOf(newAccount("other"));
  const refused = await other.GET("/v1/memories/{memory_id}", { params: { path: { memory_id: mine.id } } });
  expect(refused.response.status).toBe(404);
  const listed = await other.GET("/v1/memories", { params: { query: { scope: "personal" } } });
  expect(listed.data?.items.map((memory) => memory.id)).not.toContain(mine.id);
});

test("the memories pages fit 375 px without sideways scrolling", async ({ page, admin, signInAs }) => {
  const { project, reader, authorApi } = await seededProject(admin);
  const wide = [
    "| environment | owner | window | notes |",
    "| --- | --- | --- | --- |",
    "| production-eu-central | platform-team | Monday to Thursday | a-very-long-unbroken-identifier-that-never-wraps |",
    "",
    "```",
    "make release VERSION=1.4.0 CHANNEL=stable REGION=eu-central-1 DRY_RUN=false VERBOSE=true",
    "```",
  ].join("\n");
  const memory = await putMemory(authorApi, {
    project,
    name: "a-memory-file-with-a-rather-long-name-to-wrap.md",
    body: memoryFile("Wide content", "Tables and code", wide),
  });
  await signInAs(reader);
  await page.setViewportSize({ width: 375, height: 812 });
  for (const path of [`/p/${project}/memories`, `/p/${project}/memories/${memory.id}`, "/memories"]) {
    await page.goto(path);
    await expect(page.locator("#main").getByRole("heading", { level: 1 })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `${path} scrolls sideways`).toBeLessThanOrEqual(0);
  }
  // Wide Markdown scrolls inside its own focusable region instead.
  await page.goto(`/p/${project}/memories/${memory.id}`);
  const table = page.locator("#main").getByRole("region", { name: "Bảng" });
  await expect(table).toBeVisible();
  expect(await table.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
});
