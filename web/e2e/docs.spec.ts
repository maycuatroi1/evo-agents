import type { Locator, Page } from "@playwright/test";

import { PIXEL_PNG } from "./support/agents";
import { signOut } from "./support/auth";
import { expect, isDeployed, test } from "./support/fixtures";
import { HubAdmin, newAccount } from "./support/hub";
import { apiOf } from "./support/memories";
import { revealNavItem } from "./support/nav";
import { docsPath, enableDocs, GUIDE_TITLE, guidePath, seedGuide } from "./support/docs";
import { toast } from "./support/toast";

/**
 * The hub's feature docs in the browser, against the real API: a project the feature is on in has Docs in its menu;
 * a writer makes a document in the web with an image the hub keeps and serves, saves a second version, and reads the
 * first from its history; a reader reads every version with its images and finds no control that writes; a project
 * the feature is off in has no menu item, and its page and API answer 404.
 */
test.skip(isDeployed, "enables the feature and seeds documents through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

/** The image is drawn: the browser fetched it from the hub and decoded it. */
async function expectLoaded(image: Locator) {
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((element) => (element as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
}

test("Docs shows only where a project turned it on: no menu item, and its page and API answer 404 elsewhere", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const api = await apiOf(me);
  await page.goto(docsPath(project, "/documents"));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  const off = await api.GET("/v1/x/docs/{project}/documents", { params: { path: { project } } });
  expect(off.response.status).toBe(404);

  await enableDocs(me, project);
  await page.goto(`/p/${project}`);
  await revealNavItem(page, "nav-plugin-docs-documents");
  await page.getByTestId("nav-plugin-docs-documents").click();
  await expect(page).toHaveURL(docsPath(project, "/documents"));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Docs");
  await expect(main(page).getByTestId("docs-documents-empty")).toBeVisible();
});

test("a writer makes a document with an image in the web, saves a second version and reads the first", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await enableDocs(me, project);
  await page.goto(docsPath(project, "/documents"));
  await main(page).getByTestId("docs-new").first().click();
  await expect(page).toHaveURL(docsPath(project, "/new"));
  const editor = main(page).getByTestId("docs-editor");
  await editor.getByTestId("docs-editor-title").fill(GUIDE_TITLE);
  await expect(editor.getByTestId("docs-editor-slug")).toHaveValue("coordination-guide");
  await editor.getByTestId("docs-editor-body").fill("## The board\n\nOpen it first.\n");
  await editor.getByTestId("docs-editor-files").setInputFiles({ name: "board.png", mimeType: "image/png", buffer: PIXEL_PNG });
  await expect(editor.getByTestId("docs-editor-body")).toHaveValue(/!\[board\]\(images\/board\.png\)/);
  await editor.getByTestId("docs-editor-preview-tab").click();
  await expectLoaded(editor.getByTestId("docs-editor-preview").getByRole("img", { name: "board" }));
  await editor.getByTestId("docs-editor-save").click();
  await expect(toast(page, "Saved version 1")).toBeVisible();
  await expect(page).toHaveURL(docsPath(project, "/documents/coordination-guide"));
  const image = main(page).getByTestId("docs-body").getByRole("img", { name: "board" });
  await expectLoaded(image);
  await expect(image).toHaveAttribute("src", new RegExp(`^/v1/x/docs/${project}/documents/coordination-guide/images/[0-9a-f]{64}$`));

  await main(page).getByTestId("docs-edit").click();
  await main(page).getByTestId("docs-editor-body").fill("## The board\n\nOpen it first, then the chat.\n\n![board](images/board.png)\n");
  await main(page).getByTestId("docs-editor-summary").fill("Added the chat");
  await main(page).getByTestId("docs-editor-save").click();
  await expect(toast(page, "Saved version 2")).toBeVisible();
  await expect(main(page).getByTestId("docs-document-head")).toContainText("Version 2 of 2");
  await main(page).getByTestId("docs-tab-history").click();
  const rows = main(page).getByTestId("docs-versions-table").locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.first()).toContainText("Added the chat");
  await rows.last().getByRole("link", { name: "v1" }).click();
  await expect(main(page).getByTestId("docs-older-version")).toContainText("You are reading version 1; the latest is version 2.");
  await expect(main(page).getByTestId("docs-body")).not.toContainText("then the chat");
});

test("a reader reads a document and its images and finds no control that writes", async ({ page, member, signInAs }) => {
  const owner = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = owner.projects;
  const reader = newAccount("docs-reader");
  await new HubAdmin().grant(project, reader.login, "reader", "internal");
  await enableDocs(owner, project);
  await seedGuide(owner, project);
  await signOut(page);
  await signInAs(reader);
  await page.goto(docsPath(project, "/documents"));
  await expect(main(page).getByTestId("docs-read-only")).toBeVisible();
  await expect(main(page).getByTestId("docs-new")).toHaveCount(0);
  await main(page).getByRole("link", { name: GUIDE_TITLE }).click();
  await expect(page).toHaveURL(guidePath(project));
  await expectLoaded(main(page).getByRole("img", { name: "The board" }));
  await expect(main(page).getByTestId("markdown-image")).toContainText("Apps > Tasks");
  await expect(main(page).getByTestId("docs-edit")).toHaveCount(0);
  await page.goto(guidePath(project, "/edit"));
  await expect(main(page).getByTestId("docs-read-only")).toBeVisible();
  await expect(main(page).getByTestId("docs-editor")).toHaveCount(0);
});
