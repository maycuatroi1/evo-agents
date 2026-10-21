import path from "node:path";

import { agentAsks, authorWorker, dispatchAuthor, ensureAuthorSkill, startAuthor } from "./support/author";
import { expect, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { askDecision, runPath } from "./support/runs";

/**
 * Review screenshots of an author run's Chat with its decisions as the kit's DecisionCard: while they wait for the
 * owner, and once both are answered, at 1440x900 in light and dark, written to E2E_SCREENSHOT_DIR. Skipped unless it
 * is set; like decisions-screenshots.spec.ts it asserts nothing beyond the page being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

test.describe("author run decision screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(120_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`author run chat decisions in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width: 1440, height: 900 });
      await ensureAuthorSkill();
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      const live = await authorWorker(me, project, uniqueName("desk"));
      const run = await dispatchAuthor(me, project, live.worker.id, "Write a plan for the decisions of author runs.");
      await startAuthor(live, run.id);
      await askDecision(live, run.id, "Does the first revision cover author runs, or plan runs only?", undefined, {
        category: "scope",
        step_key: null,
        context: "Plan runs already ask decisions; author runs had **none** until now.",
        options: [
          { key: "both", label: "Author runs as well", description: "One more step for the chat's cards." },
          { key: "plan", label: "Plan runs only", description: "Author runs wait for the next plan." },
        ],
        recommended: "both",
      });
      await askDecision(live, run.id, "Where do the answers go: the chat's messages, or the decisions alone?", undefined, {
        category: "architecture",
        step_key: null,
        context: null,
        options: [
          { key: "decisions", label: "The decisions alone" },
          { key: "chat", label: "The chat's messages too" },
        ],
        recommended: "decisions",
      });
      await agentAsks(live, run.id, "Before I write the plan I asked you two things; the cards are below.");

      await open(page, runPath(project, run.id));
      const chat = page.locator("#main").getByTestId("run-chat");
      const cards = chat.getByTestId("run-chat-decision");
      await expect(cards).toHaveCount(2);
      await expect(cards.nth(1).getByTestId("decision-send")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `author-chat-decisions-waiting-${scheme}.png`) });

      await cards.nth(0).getByTestId("decision-text").fill("Both, but keep the cards compact.");
      await cards.nth(0).getByTestId("decision-send").click();
      await expect(cards.nth(0).getByTestId("decision-answer")).toBeVisible();
      await cards.nth(1).getByTestId("decision-send").click();
      await expect(cards.nth(1).getByTestId("decision-answer")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `author-chat-decisions-answered-${scheme}.png`) });
    });
  }
});
