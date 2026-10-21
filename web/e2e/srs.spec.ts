import type { Page } from "@playwright/test";

import { call } from "../src/lib/api/client";

import { signOut } from "./support/auth";
import { STACK_URL } from "./support/env";
import { expect, isDeployed, test } from "./support/fixtures";
import { HubAdmin, newAccount } from "./support/hub";
import { seededKg } from "./support/kg";
import { apiOf } from "./support/memories";
import {
  baEnds,
  baFinding,
  baRecords,
  baStarts,
  BILLING_SLUG,
  DECIDE_SUMMARY,
  deliverPluginEvents,
  documentPath,
  editItem,
  enableBa,
  enableSrs,
  enableTasks,
  findingsOf,
  importBilling,
  importSample,
  itemPath,
  RETRIES,
  reviewVersion,
  RULE,
  SAMPLE_SLUG,
  SAMPLE_SRS,
  srsPath,
  submitVersion,
  USE_CASE,
} from "./support/srs";
import { toast } from "./support/toast";

/**
 * Criterion a5 of plan srs-tasks-plugins in the browser, against the real API: a project that enabled the core plugin
 * srs has SRS in its menu, the documents, a document's contents with each section read and edited in place, a use
 * case's page with its steps, business rules, screens and trace in the knowledge graph, the review that approves a
 * version or returns it, and the comparison of two versions; a reader only reads; a project that has not enabled it
 * has no menu item, and its pages and API answer 404. Criterion a14 of plan srs-conflicts-and-tasks: the review page's
 * Findings card, and Approve locked while @ba reviews the round or a finding to decide is open, with its settle chat.
 */
test.skip(isDeployed, "enables plugins and seeds documents through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("SRS shows only where a project enabled it: no menu item, and its page and API answer 404 elsewhere", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const api = await apiOf(me);
  await page.goto(srsPath(project));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  await page.goto(`/p/${project}`);
  await expect(page.getByTestId("nav-plugin-srs-documents")).toHaveCount(0);
  const off = await api.GET("/v1/x/srs/{project}/documents", { params: { path: { project } } });
  expect(off.response.status).toBe(404);

  await enableSrs(me, project);
  await page.reload();
  await page.getByTestId("nav-plugin-srs-documents").click();
  await expect(page).toHaveURL(srsPath(project));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("SRS");
  await expect(main(page).getByTestId("srs-documents-empty")).toBeVisible();
  const on = await api.GET("/v1/x/srs/{project}/documents", { params: { path: { project } } });
  expect(on.response.status).toBe(200);
});

test("a writer imports Markdown after its preview, reads the contents and a use case, edits it into a draft and submits it", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await enableSrs(me, project);

  await page.goto(srsPath(project));
  await main(page).getByTestId("srs-import-open").click();
  await expect(page).toHaveURL(`/p/${project}/x/srs/import`);
  await expect(main(page).getByTestId("srs-import-run")).toBeDisabled();
  await main(page).getByLabel("Slug").fill(SAMPLE_SLUG);
  await main(page).getByTestId("srs-import-markdown").fill(SAMPLE_SRS);
  await main(page).getByTestId("srs-import-preview-run").click();
  const preview = main(page).getByTestId("srs-import-preview");
  await expect(preview.getByTestId("srs-import-verdict")).toHaveText("Ready to import as a draft");
  await expect(preview.getByTestId("srs-import-counts")).toContainText("Use case: 3");
  await expect(preview.getByTestId("srs-import-outline")).toContainText(USE_CASE.title);
  await main(page).getByTestId("srs-import-run").click();
  await expect(toast(page, `Imported ${SAMPLE_SLUG} as v1`)).toBeVisible();
  await expect(page).toHaveURL(documentPath(project, "?v=1"));

  // The contents: the tree of headings, the use case drawn from its fields, and its own page.
  const toc = main(page).getByTestId("srs-toc");
  await toc.getByRole("link", { name: new RegExp(USE_CASE.title) }).click();
  await expect(main(page).getByTestId("srs-item-title")).toHaveText(USE_CASE.title);
  await expect(toc.getByRole("link", { name: new RegExp(USE_CASE.title) })).toHaveAttribute("aria-current", "true");
  await expect(main(page).getByTestId("srs-steps")).toContainText("Checks the cart");
  await expect(main(page).getByTestId("srs-rule").first()).toContainText(RULE.title);
  await main(page).getByTestId("srs-item-page").click();
  await expect(page).toHaveURL(itemPath(project, USE_CASE.code) + "?v=1");
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText(USE_CASE.title);
  await expect(main(page).getByTestId("srs-item-code")).toHaveText(USE_CASE.code);
  await expect(main(page).getByTestId("srs-trace-no-graph")).toBeVisible();
  await main(page).getByTestId("srs-steps").getByRole("link", { name: RULE.code }).click();
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText(RULE.title);

  // Edit the use case in place: the save is v2, a draft, which its writer sends to review.
  await page.goto(documentPath(project, `?s=${encodeURIComponent(USE_CASE.code)}`));
  await main(page).getByTestId("srs-edit").click();
  const editor = main(page).getByTestId("srs-editor");
  await expect(editor).toContainText("Saving makes v2, a draft built on v1.");
  const body = editor.getByTestId("srs-editor-body");
  await body.fill(`${await body.inputValue()}\n\nOrders over 50 lines are split.`);
  await editor.getByTestId("srs-editor-summary").fill("Split large orders");
  await editor.getByTestId("srs-editor-save").click();
  await expect(toast(page, "Saved v2, a draft")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`\\?v=2&s=`));
  await expect(main(page).getByTestId("srs-item")).toContainText("Orders over 50 lines are split.");
  await expect(main(page).getByTestId("srs-draft")).toContainText(`v2 is a draft by ${me.login}`);
  await main(page).getByTestId("srs-submit").click();
  await expect(toast(page, "Sent v2 to review")).toBeVisible();
  await expect(main(page).getByTestId("srs-draft")).toHaveCount(0);
  await expect(main(page).getByTestId("srs-document-tabs").getByTestId("srs-tab-review")).toHaveText("Review v2");
});

test("another writer returns and then approves a version its author cannot review; a reader reads, compares and comments nowhere", async ({
  page,
  member,
  signInAs,
}) => {
  const author = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = author.projects;
  const reviewer = newAccount("srs-reviewer");
  const reader = newAccount("srs-reader");
  const hub = new HubAdmin();
  await hub.grant(project, reviewer.login, "writer", "internal");
  await hub.grant(project, reader.login, "reader", "internal");
  await enableSrs(author, project);
  await importSample(author, project);
  const v2 = await editItem(author, project, USE_CASE.code, (held) => `${held}\n\nThe shop keeps the cart for a day.`);
  await submitVersion(author, project, v2);

  // Its author cannot review it, admin as they are.
  await page.goto(documentPath(project, `/versions/${v2}`));
  await expect(main(page).getByTestId("srs-review-blocked")).toContainText(`You wrote v1, v2 since the first version`);
  await expect(main(page).getByTestId("srs-review-form")).toHaveCount(0);

  // Another writer returns it with a note, then approves it once it is back in review.
  await signOut(page);
  await signInAs(reviewer);
  await page.goto(documentPath(project, `/versions/${v2}`));
  const form = main(page).getByTestId("srs-review-form");
  await form.getByRole("radio", { name: /Request changes/ }).click();
  await form.getByTestId("srs-review-submit").click();
  await expect(form.getByRole("alert")).toHaveText("Say what to change.");
  await form.getByTestId("srs-review-note").fill("Name the hold time.");
  await form.getByTestId("srs-review-submit").click();
  await expect(toast(page, `Returned v${v2} to draft`)).toBeVisible();
  await expect(main(page).getByTestId("srs-draft")).toContainText(`${reviewer.login} returned v${v2} to draft`);
  await expect(main(page).getByTestId("srs-review-history")).toContainText("Name the hold time.");
  await submitVersion(author, project, v2);
  await page.reload();
  await main(page).getByTestId("srs-review-form").getByTestId("srs-review-submit").click();
  await expect(toast(page, `Approved v${v2}`)).toBeVisible();
  await expect(main(page).getByTestId("srs-review-approved")).toBeVisible();

  // The reviewer comments on the use case and resolves the thread.
  await page.goto(itemPath(project, USE_CASE.code));
  await main(page).getByTestId("srs-comment-form").getByRole("textbox").fill("Is a day long enough?");
  await main(page).getByTestId("srs-comment-submit").click();
  await expect(toast(page, "Comment added")).toBeVisible();
  const thread = main(page).getByTestId("srs-thread");
  await expect(thread).toContainText("Is a day long enough?");
  await thread.getByTestId("srs-thread-resolve").click();
  await expect(thread.getByTestId("srs-thread-state")).toHaveText(`Resolved by ${reviewer.login}`);

  // A reader reads the approved version, compares v1 with v2, and finds no control that writes.
  await signOut(page);
  await signInAs(reader);
  await page.goto(documentPath(project));
  await expect(main(page).getByTestId("srs-read-only")).toBeVisible();
  await expect(main(page).getByTestId("srs-version-menu")).toHaveText(/Showing v2: Approved/);
  await expect(main(page).getByTestId("srs-edit")).toHaveCount(0);
  await page.goto(documentPath(project, "/versions"));
  await expect(main(page).getByTestId("srs-versions-table").locator("tbody tr")).toHaveCount(2);
  await main(page).getByTestId("srs-compare-open").click();
  await expect(page).toHaveURL(documentPath(project, "/compare?from=1&to=2"));
  const changed = main(page).getByTestId("srs-diff-item").filter({ hasText: USE_CASE.title });
  await expect(changed).toHaveAttribute("data-change", "changed");
  await expect(changed.locator('[data-kind="added"]', { hasText: "The shop keeps the cart for a day." })).toHaveCount(1);
  await expect(main(page).getByTestId("srs-diff-counts")).toContainText("Changed");
  await page.goto(itemPath(project, USE_CASE.code));
  await expect(main(page).getByTestId("srs-thread")).toContainText("Is a day long enough?");
  await expect(main(page).getByTestId("srs-comment-form")).toHaveCount(0);
  await expect(main(page).getByTestId("srs-item-edit")).toHaveCount(0);
  await page.goto(documentPath(project, `/versions/${v2}`));
  await expect(main(page).getByTestId("srs-review-form")).toHaveCount(0);
  await page.goto(`/p/${project}/x/srs/import`);
  await expect(main(page).getByTestId("srs-import-reader")).toBeVisible();
});

test("a use case's page traces the task that implements it through the project's knowledge graph", async ({ page, admin, signInAs }) => {
  const { project, writer } = await seededKg(admin);
  const approver = newAccount("srs-approver");
  await admin.grant(project, writer.login, "admin", "customer");
  await admin.grant(project, approver.login, "writer", "customer");
  await enableSrs(writer, project);
  await enableTasks(writer, project);
  const v1 = await importSample(writer, project);
  await submitVersion(writer, project, v1);
  await reviewVersion(approver, project, v1, "approve");
  const api = await apiOf(writer);
  const title = "Checkout places the order";
  await call(
    api.POST("/v1/x/tasks/{project}/tasks", {
      params: { path: { project } },
      body: { title, description_md: "", priority: "medium", links: { use_cases: [USE_CASE.code] } },
    }),
  );
  // The approval and the task each queued a build; the stack runs the one that waits, as the worker would.
  const built = await fetch(`${STACK_URL}/kg/build`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ project, login: writer.login, id: writer.id, run: true }),
  });
  const answer = await built.text();
  expect(built.ok, answer).toBe(true);
  expect((JSON.parse(answer) as { status: string }).status, answer).toBe("succeeded");

  await signInAs(writer);
  await page.goto(itemPath(project, USE_CASE.code));
  const trace = main(page).getByTestId("srs-trace");
  await expect(trace.getByTestId("srs-trace-groups")).toContainText(title);
  await expect(trace.getByTestId("srs-trace-node")).toHaveAttribute("href", `/p/${project}/kg/node?id=${encodeURIComponent(`usecase:${USE_CASE.code}`).replace(/%20/g, "+")}`);
});

test("@ba's review holds Approve: the card fills as @ba records, the finding to decide names its settle chat, and once settled the version is approved", async ({
  page,
  member,
  signInAs,
}) => {
  test.setTimeout(120_000);
  const author = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = author.projects;
  const reviewer = newAccount("srs-approver");
  const reader = newAccount("srs-reader");
  const hub = new HubAdmin();
  await hub.grant(project, reviewer.login, "writer", "internal");
  await hub.grant(project, reader.login, "reader", "internal");
  await enableSrs(author, project);
  await importBilling(author, project);
  const ba = await enableBa(author, project, author);
  const v1 = await importSample(author, project);
  const sent = await submitVersion(author, project, v1);
  expect(sent.review_run?.state).toBe("queued");

  // The reviewer opens the review while @ba's run waits for a worker: Approve waits for it, Request changes does not.
  await signOut(page);
  await signInAs(reviewer);
  await page.goto(documentPath(project, `/versions/${v1}`));
  const card = main(page).getByTestId("srs-findings");
  const form = main(page).getByTestId("srs-review-form");
  const approve = form.getByRole("radio", { name: /^Approve/ });
  await expect(card.getByTestId("srs-findings-reviewing")).toContainText("@ba is reviewing round 1");
  await expect(card.getByTestId("srs-findings-empty")).toContainText("@ba has recorded none so far.");
  await expect(approve).toBeDisabled();
  await expect(form.getByRole("radio", { name: /^Request changes/ })).toBeChecked();
  await expect(form.getByTestId("srs-review-lock")).toContainText("Approve waits for @ba's review of round 1");
  await expect(form.getByTestId("srs-review-run")).toHaveAttribute("data-run-id", String(sent.review_run!.id));

  // @ba's run starts and records a conflict to decide and a duplicate: the card shows them while the run goes on.
  const review = await baStarts(ba, "task");
  expect(review).toBe(sent.review_run!.id);
  const decide = await baRecords(ba, review, {
    kind: "contradiction",
    level: "decide",
    item: RETRIES.code,
    summary: DECIDE_SUMMARY,
    sources: [{ kind: "code", ref: RETRIES.billing, note: "billing retries 5 times in 30 minutes" }],
  });
  const advice = await baRecords(ba, review, {
    kind: "duplicate",
    level: "advisory",
    item: USE_CASE.code,
    summary: "Placing an order repeats billing's checkout.",
    sources: [{ kind: "code", ref: "UC BIL-1" }],
  });
  const toDecide = card.getByTestId("srs-findings-decide");
  const row = toDecide.getByTestId("srs-finding");
  await expect(row).toHaveAttribute("data-finding", String(decide), { timeout: 15_000 });
  await expect(toDecide.getByTestId("srs-findings-decide-count")).toHaveText("1");
  await expect(card.getByTestId("srs-findings-open").getByTestId("srs-finding")).toHaveAttribute("data-finding", String(advice));
  await expect(row.getByTestId("srs-finding-summary")).toHaveText(DECIDE_SUMMARY);
  await expect(row.getByRole("link", { name: RETRIES.code })).toHaveAttribute("href", `${itemPath(project, RETRIES.code)}?v=${v1}`);
  await expect(row.getByRole("link", { name: RETRIES.billing })).toHaveAttribute("href", itemPath(project, RETRIES.billing, BILLING_SLUG));
  await expect(form.getByTestId("srs-review-lock")).toContainText("Approve is locked: 1 finding to decide is open");
  await expect(approve).toBeDisabled();

  // The run ends and the hub opens the settle chat, which the open finding to decide and the lock link to.
  await baEnds(ba, review, "One conflict to decide, a duplicate.");
  expect((await deliverPluginEvents(project)).delivered).toBeGreaterThan(0);
  const chat = (await findingsOf(reviewer, project)).find((one) => one.id === decide)?.chat_link ?? "";
  expect(chat).toMatch(/^\/chats\/\d+\?message=\d+$/);
  await page.reload();
  await expect(card.getByTestId("srs-findings-reviewing")).toHaveCount(0);
  await expect(row.getByTestId("srs-finding-chat")).toHaveAttribute("href", chat);
  await expect(form.getByTestId("srs-review-lock-chat")).toHaveAttribute("href", chat);
  await expect(approve).toBeDisabled();

  // A reader sees the card and no form.
  await signOut(page);
  await signInAs(reader);
  await page.goto(documentPath(project, `/versions/${v1}`));
  await expect(card.getByTestId("srs-findings-decide").getByTestId("srs-finding")).toHaveAttribute("data-finding", String(decide));
  await expect(main(page).getByTestId("srs-review-blocked")).toBeVisible();
  await expect(main(page).getByTestId("srs-review-form")).toHaveCount(0);

  // @ba's turn in the settle chat resolves the conflict as the reviewer decided there: Approve is free, and approves.
  const turn = await baStarts(ba, "chat");
  await baFinding(ba, turn, { action: "resolve", finding: decide, resolution: "Keep 3 retries; billing moves to 3.", settled_by: reviewer.login, chat_link: chat });
  await signOut(page);
  await signInAs(reviewer);
  await page.goto(documentPath(project, `/versions/${v1}`));
  const settled = card.getByTestId("srs-findings-settled").getByTestId("srs-finding");
  await expect(settled).toHaveAttribute("data-finding", String(decide));
  await expect(settled.getByTestId("srs-finding-settler")).toHaveText(`Settled by ${reviewer.login}`);
  await expect(settled).toContainText("Keep 3 retries; billing moves to 3.");
  await expect(settled.getByTestId("srs-finding-chat")).toHaveAttribute("href", chat);
  await expect(card.getByTestId("srs-findings-decide")).toHaveCount(0);
  await expect(form.getByTestId("srs-review-lock")).toHaveCount(0);
  await expect(approve).toBeEnabled();
  await expect(approve).toBeChecked();
  await form.getByTestId("srs-review-submit").click();
  await expect(toast(page, `Approved v${v1}`)).toBeVisible();
  await expect(main(page).getByTestId("srs-review-approved")).toBeVisible();
});
