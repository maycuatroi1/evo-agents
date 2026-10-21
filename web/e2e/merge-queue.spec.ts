import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount } from "./support/hub";
import { GITHUB_REPO, gitlabRepos, mergeQueuePath, OTHER, REPO, seedMergeQueue, seedPolicy } from "./support/merge-queue";
import { open } from "./support/plans";
import { toast } from "./support/toast";

/**
 * The merge queue on GitLab and the autonomy policy on the web (plan gitlab-merge-queue, criterion a8), against the
 * real API with the queue the hub's job would leave (e2e/merge_seed.py): the queue page lists the merge requests that
 * wait, merge, are blocked and are followed after their merge, why each waits, its ledger, the breaker of each repo and
 * the audit samples waiting for an answer; an admin resets a breaker, answers a sample, approves a PROD release and asks
 * for a revert, there and from the Inbox; the policy page shows the policy, its revisions and a comparison of two, and
 * an admin writes the next revision through its form, which refuses what the hub would. A reader reads and changes
 * nothing.
 */
test.skip(isDeployed, "seeds the merge queue through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("an admin reads the queue, opens a merge request, approves its release, answers a sample and resets a breaker", async ({ page, member, admin }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await gitlabRepos(admin, project);
  await seedPolicy(me, project);
  const seeded = await seedMergeQueue(project, { admin: me.login });

  await open(page, mergeQueuePath(project));
  await expect(main(page).getByRole("heading", { level: 1, name: "Merge queue" })).toBeVisible();
  await expect(page.getByTestId("nav-mergeQueue")).toHaveAttribute("aria-current", "page");
  const banner = main(page).getByTestId("breaker-banner");
  await expect(banner).toHaveCount(1);
  await expect(banner).toContainText(`Merges of ${OTHER} stopped: a UAT check failed`);
  await expect(main(page).getByTestId("merge-queue-summary").getByTestId("summary-blocked")).toContainText("1");
  const table = main(page).getByTestId("merge-queue-table");
  await expect(table.locator("tbody tr")).toHaveCount(7);
  await expect(main(page).getByTestId("merge-queue-tab-queue")).toHaveAttribute("aria-current", "page");
  await expectNoSeriousViolations(page, "merge queue as an admin");

  // Where each one stands: the blocked one says why, the filter keeps it alone and the URL says so.
  await main(page).getByTestId("merge-queue-groups").locator('[data-facet-value="blocked"]').click();
  await expect(page).toHaveURL(/state=blocked/);
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.getByTestId("merge-row-reason")).toContainText("gate 3 (score hacking)");
  await main(page).getByTestId("merge-queue-groups").locator('[data-facet-value=""]').click();
  await expect(table.locator("tbody tr")).toHaveCount(7);

  // The merge request in its turn: rebased onto the target branch's head, both Judges, and its ledger.
  await table.getByTestId("merge-row-open").filter({ hasText: "Add the invoice export" }).click();
  await expect(page).toHaveURL(new RegExp(`mr=${seeded.requests["41"]}`));
  const sheet = page.getByTestId("merge-request-sheet");
  await expect(sheet.getByTestId("merge-state")).toHaveText("Merging");
  await expect(sheet.getByTestId("merge-request-before")).toContainText("9d8c7b6a");
  await expect(sheet.getByTestId("merge-request-before")).toContainText("1 rebase in a row");
  await expect(sheet.getByTestId("merge-judge")).toHaveCount(2);
  await expect(sheet.getByTestId("merge-risk")).toHaveAttribute("data-high", "true");
  await expect(sheet.getByTestId("merge-event")).toHaveCount(8);
  await expect(sheet.getByTestId("merge-actions")).toHaveCount(0);
  await expectNoSeriousViolations(page, "a merge request's sheet");
  await sheet.getByTestId("merge-request-close").click();
  await expect(sheet).toBeHidden();

  // Its PROD release waits for an admin, since the policy releases shop by hand.
  await open(page, `${mergeQueuePath(project)}?mr=${seeded.requests["40"]}`);
  await expect(sheet.getByTestId("merge-state")).toHaveText("Releasing");
  await sheet.getByTestId("merge-release").click();
  await expect(toast(page, "PROD release of !40 approved")).toBeVisible();

  // A merge it followed is reverted on an admin's word, after a confirmation.
  await open(page, `${mergeQueuePath(project)}?mr=${seeded.requests["39"]}`);
  await sheet.getByTestId("merge-revert").click();
  await page.getByTestId("merge-revert-dialog-confirm").click();
  await expect(toast(page, "Revert of !39 asked")).toBeVisible();
  await expect(sheet.getByTestId("merge-state")).toHaveText("Reverting");
  await page.keyboard.press("Escape");

  // The audit sample waiting for an answer, OK at once.
  const samples = main(page).getByTestId("samples");
  const waiting = samples.locator(`[data-sample-id="${seeded.sample}"]`);
  await expect(waiting).toHaveAttribute("data-result", "waiting");
  await expect(samples.locator(`[data-sample-id="${seeded.answered}"]`)).toContainText(`${me.login} answered`);
  await waiting.getByTestId("sample-ok").click();
  await expect(toast(page, "!39 shop: marked OK")).toBeVisible();
  await expect(waiting).toHaveAttribute("data-result", "ok");

  // The tripped breaker, reset after a confirmation: its paused merge request waits for its turn again.
  await banner.getByTestId("breaker-reset").click();
  await expect(page.getByTestId("breaker-reset-dialog")).toBeVisible();
  await expectNoSeriousViolations(page, "reset breaker confirmation");
  await page.getByTestId("breaker-reset-dialog-confirm").click();
  await expect(toast(page, `Breaker of ${OTHER} reset`)).toBeVisible();
  await expect(main(page).getByTestId("breaker-banner")).toHaveCount(0);
  await expect(main(page).getByTestId("breakers").locator(`[data-repo="${OTHER}"]`)).toHaveAttribute("data-state", "armed");
  await expect(table.locator('[data-testid="merge-state"][data-status="paused"]')).toHaveCount(0);
});

test("a reader reads the queue and the policy and changes nothing", async ({ page, member, admin }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];
  const owner = newAccount("mq-owner");
  await admin.grant(project, owner.login, "admin", "internal");
  await gitlabRepos(admin, project);
  await seedPolicy(owner, project);
  const seeded = await seedMergeQueue(project, { admin: owner.login });

  await open(page, mergeQueuePath(project));
  await expect(main(page).getByTestId("merge-queue-read-only")).toBeVisible();
  await expect(main(page).getByTestId("breaker-banner")).toContainText("An admin of the project resets it");
  await expect(main(page).getByTestId("breaker-reset")).toHaveCount(0);
  await expect(main(page).getByTestId("sample-ok")).toHaveCount(0);
  await open(page, `${mergeQueuePath(project)}?mr=${seeded.requests["40"]}`);
  await expect(page.getByTestId("merge-request-sheet").getByTestId("merge-state")).toHaveText("Releasing");
  await expect(page.getByTestId("merge-request-sheet").getByTestId("merge-release")).toHaveCount(0);

  await open(page, mergeQueuePath(project, "/policy"));
  await expect(main(page).getByTestId("policy-read-only")).toBeVisible();
  await expect(main(page).getByTestId("policy-edit")).toHaveCount(0);
  await expect(main(page).getByTestId("policy-view")).toHaveAttribute("data-revision", "2");
  await open(page, `${mergeQueuePath(project, "/policy")}?edit=1`);
  await expect(main(page).getByTestId("policy-form")).toHaveCount(0);
  await expect(main(page).getByTestId("policy-view")).toBeVisible();
});

test("an admin writes the policy through its form, reads its revisions and compares two", async ({ page, member, admin }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await gitlabRepos(admin, project);
  await seedPolicy(me, project);

  await open(page, mergeQueuePath(project, "/policy"));
  const view = main(page).getByTestId("policy-view");
  await expect(view).toHaveAttribute("data-revision", "2");
  await expect(view.locator(`[data-testid="policy-repo"][data-repo="${REPO}"]`)).toHaveAttribute("data-mode", "auto");
  await expect(view.locator(`[data-testid="policy-repo"][data-repo="${GITHUB_REPO}"]`)).toHaveAttribute("data-mode", "off");
  await expect(main(page).getByTestId("policy-revision")).toHaveCount(2);
  // The comparison of the newest with the one before: what revision 2 changed.
  const changes = main(page).getByTestId("policy-compare").getByTestId("policy-change");
  await expect(changes).toHaveCount(3);
  await expect(main(page).getByTestId("policy-compare").locator('[data-key="repos.shop.prod_release"]')).toContainText("By hand");
  await expectNoSeriousViolations(page, "policy with its revisions and a comparison");

  // An older revision, then back.
  await main(page).getByTestId("policy-revision-link").last().click();
  await expect(main(page).getByTestId("policy-old-revision")).toBeVisible();
  await expect(view).toHaveAttribute("data-revision", "1");
  await main(page).getByTestId("policy-show-newest").click();
  await expect(view).toHaveAttribute("data-revision", "2");

  // The form refuses what the hub would, and auto on github.com is not offered.
  await main(page).getByTestId("policy-edit").click();
  await expect(page).toHaveURL(/edit=1/);
  const form = main(page).getByTestId("policy-form");
  const github = form.locator(`[data-testid="policy-form-repo"][data-repo="${GITHUB_REPO}"]`);
  await expect(github.locator('option[value="auto"]')).toBeDisabled();
  await form.getByTestId("policy-field-policy-breaker.target_red_minutes").fill("0");
  await form.getByTestId("policy-field-policy-audit.sample_rate").fill("2");
  await form.getByTestId("policy-save").click();
  await expect(form.getByTestId("policy-form-problems")).toHaveText("2 fields need a fix.");
  await expect(form.getByTestId("policy-field-policy-breaker.target_red_minutes")).toBeFocused();
  await expect(form.getByTestId("policy-error-policy-breaker.target_red_minutes")).toHaveText("A whole number from 1 to 10080.");
  await expectNoSeriousViolations(page, "policy form with errors");

  await form.getByTestId("policy-field-policy-breaker.target_red_minutes").fill("45");
  await form.getByTestId("policy-field-policy-audit.sample_rate").fill("0.25");
  const billing = form.locator(`[data-testid="policy-form-repo"][data-repo="${OTHER}"]`);
  await billing.locator("select").first().selectOption("auto");
  await form.getByTestId("policy-note").fill("billing joins the queue");
  await form.getByTestId("policy-save").click();
  await expect(toast(page, "Revision 3 saved")).toBeVisible();
  await expect(page).not.toHaveURL(/edit=1/);
  await expect(view).toHaveAttribute("data-revision", "3");
  await expect(view.locator(`[data-testid="policy-repo"][data-repo="${OTHER}"]`)).toHaveAttribute("data-mode", "auto");
  await expect(main(page).getByTestId("policy-provenance")).toContainText(`Revision 3 by ${me.login}`);
  await expect(main(page).getByTestId("policy-provenance")).toContainText("billing joins the queue");
  await expect(changes).toHaveCount(3); // target_red_minutes, sample_rate, billing's mode
});

test("an admin answers the queue's notices from the Inbox", async ({ page, member, admin }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await gitlabRepos(admin, project);
  await seedPolicy(me, project);
  await seedMergeQueue(project, { admin: me.login, notify: me.login });

  await open(page, "/inbox");
  const notices = main(page).getByTestId("notification");
  const sample = notices.filter({ has: page.locator('[data-testid="merge-notice"][data-ask="sample"]') });
  const paused = notices.filter({ has: page.locator('[data-testid="merge-notice"][data-ask="resume"]') });
  const release = notices.filter({ has: page.locator('[data-testid="merge-notice"][data-ask="release"]') });
  await expect(paused).toHaveAttribute("data-notice-kind", "merge_paused");
  await expect(paused.getByTestId("notice-kind")).toHaveText("The merge queue of a repo stopped");
  await expect(release.getByTestId("merge-notice-release")).toBeVisible();
  await expect(sample.getByTestId("merge-notice-open")).toHaveAttribute("href", mergeQueuePath(project));
  await expectNoSeriousViolations(page, "inbox with the merge queue's notices");

  await sample.getByTestId("merge-notice-faulty-open").click();
  await sample.getByTestId("merge-notice-note").fill("The export drops the currency column.");
  await sample.getByTestId("merge-notice-faulty-send").click();
  await expect(toast(page, "Audit sample marked faulty")).toBeVisible();
  await expect(sample.getByTestId("merge-notice-status")).toHaveText(`Answered faulty by ${me.login}.`);

  await paused.getByTestId("merge-notice-resume").click();
  await page.getByTestId("merge-notice-resume-dialog-confirm").click();
  await expect(toast(page, `Breaker of ${OTHER} reset`)).toBeVisible();
  await expect(paused.getByTestId("merge-notice-status")).toHaveText(`The breaker was reset by ${me.login}.`);
});

test("a project without a policy has an empty queue, every repo off, and the figures say so", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await open(page, mergeQueuePath(project));
  await expect(main(page).getByTestId("state-empty")).toContainText("No merge request in the queue yet");
  await main(page).getByTestId("merge-queue-empty-policy").click();
  await expect(main(page).getByTestId("policy-none")).toBeVisible();
  await expect(main(page).getByTestId("policy-provenance")).toContainText("No policy yet");
  await expect(main(page).getByTestId("policy-edit")).toHaveText("Write the policy");
  await main(page).getByTestId("merge-queue-tab-figures").click();
  await expect(main(page).getByTestId("state-empty")).toContainText("No merge in the last 30 days");
  await main(page).getByTestId("merge-figures-days-7").click();
  await expect(page).toHaveURL(/days=7/);
  await expect(main(page).getByTestId("state-empty")).toContainText("No merge in the last 7 days");
});
