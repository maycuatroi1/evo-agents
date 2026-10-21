import path from "node:path";

import type { Page } from "@playwright/test";

import { expect, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { filesOfEachKind, type OutputFile, outputLink, pageShot, sendOutputs } from "./support/outputs";
import { open } from "./support/plans";
import { askDecision, claimRun, dispatch, liveWorker, planRunUnderway, runPath, seedPlanRunPlan, seedRunPlan, sendEvents, startRun } from "./support/runs";

/**
 * Review screenshots of the files of runs (plan run-outputs, step 6): a run's Trace with the files under their calls,
 * its Outputs tab, the viewer on an image, Markdown, CSV and an HTML page, a question that carries a screenshot, the
 * Inbox's notice of a run's files and the project's Run outputs card, light and dark, at 1440 and 375 px, written to
 * E2E_SCREENSHOT_DIR. Skipped unless it is set; like screenshots.spec.ts it asserts nothing beyond the page being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

const HOME: OutputFile = { name: "home.png", media_type: "image/png", data: pageShot(1280, 800, [17, 113, 61]), source: "tool", tool_call_id: "read-home" };
const DARK: OutputFile = { name: "settings-dark.png", media_type: "image/png", data: pageShot(1280, 800, [140, 157, 255], true), source: "tool", tool_call_id: "read-settings" };

test.describe("run outputs screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(180_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`the files of runs in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "admin", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedRunPlan(me, project);
      const live = await liveWorker(me, project, uniqueName("laptop"));
      const [run] = await dispatch(me, project, ["2"]);
      await claimRun(live);
      await startRun(live, run.id);
      await sendEvents(live, run.id, [
        { kind: "agent_message_chunk", body: { text: "I'll check the home page and the settings page in the browser, then send the report." } },
        { kind: "tool_call", body: { toolCallId: "read-home", title: "Read", kind: "read", rawInput: { file_path: "/tmp/shots/home.png" }, status: "in_progress" } },
        { kind: "tool_call_update", body: { toolCallId: "read-home", status: "completed", content: [outputLink(HOME)] } },
        { kind: "tool_call", body: { toolCallId: "read-settings", title: "Read", kind: "read", rawInput: { file_path: "/tmp/shots/settings-dark.png" }, status: "in_progress" } },
        { kind: "tool_call_update", body: { toolCallId: "read-settings", status: "completed", content: [outputLink(DARK)] } },
        { kind: "tool_call", body: { toolCallId: "send", title: "Bash", kind: "execute", rawInput: { command: "evo-agents worker output report.md checkout.webm" }, status: "in_progress" } },
        { kind: "tool_call_update", body: { toolCallId: "send", status: "completed", content: [{ type: "content", content: { type: "text", text: "queued 2 files" } }] } },
        { kind: "agent_message_chunk", body: { text: "Both pages render; the report and a recording of the checkout are in the run's outputs." } },
      ]);
      const files = filesOfEachKind();
      const sent = files.filter((file) => file.name === "report.md" || file.name === "checkout.webm");
      await sendOutputs(live, run.id, [HOME, DARK, ...sent]);
      await sendOutputs(live, run.id, files.filter((file) => !sent.includes(file)));

      // A question that carries a screenshot, on a plan run.
      await seedPlanRunPlan(me, project);
      const plan = await planRunUnderway(me, project, uniqueName("asker"));
      const [shot] = await sendOutputs(plan.live, plan.run.id, [
        { name: "staging.png", media_type: "image/png", data: pageShot(1200, 750, [179, 38, 30]), caption: "Staging, before the deploy" },
        { name: "build.log", media_type: "text/plain", data: Buffer.from("built northwind 2.4.0 in 41 s\n") },
      ]);
      await askDecision(plan.live, plan.run.id, "Deploy this build to staging?", "3", { output_ids: [shot.id] });

      for (const [width, height, suffix] of [
        [1440, 900, ""],
        [375, 812, "-375"],
      ] as const) {
        await page.setViewportSize({ width, height });
        const shotOf = (name: string, full = false) => page.screenshot({ path: path.join(dir!, `outputs-${name}${suffix}-${scheme}.png`), fullPage: full });
        // The images in view have loaded; a lazy one out of view loads only when scrolled to.
        const settle = (target: Page) =>
          target.waitForFunction(() =>
            [...document.images].every((image) => {
              const box = image.getBoundingClientRect();
              const inView = box.bottom > 0 && box.top < window.innerHeight && box.width > 0;
              return (image.complete && image.naturalWidth > 0) || !inView;
            }),
          ).then(() => target.waitForTimeout(250));

        await open(page, runPath(project, run.id));
        if (width < 768) {
          await expect(page.getByTestId("run-outputs-preview")).toBeVisible();
          await expect(page.getByTestId("run-trace-preview-row").first()).toBeVisible();
          await settle(page);
          await shotOf("run-page", true);
          await page.getByTestId("run-open-trace").click();
        }
        await expect(page.getByTestId("trace-group-files").first()).toBeVisible();
        await settle(page);
        await shotOf("trace");

        await open(page, `${runPath(project, run.id)}?view=outputs`);
        await expect(page.getByTestId("run-outputs-grid")).toBeVisible();
        await settle(page);
        await shotOf("tab");

        for (const name of ["home.png", "report.md", "timings.csv", "probe.html", "release-notes.pdf", "checkout.webm"]) {
          await page.getByTestId("output-tile").filter({ hasText: name }).click();
          await expect(page.getByTestId("output-viewer-name")).toHaveText(name);
          await page.waitForTimeout(300);
          await settle(page);
          await shotOf(`viewer-${name.replace(/\W+/g, "-")}`);
          await page.keyboard.press("Escape");
        }

        await open(page, runPath(project, plan.run.id));
        await expect(page.getByTestId("decision-files")).toBeVisible();
        await settle(page);
        await shotOf("question");

        await open(page, "/inbox");
        await expect(page.getByTestId("notification-files").first()).toBeVisible();
        await settle(page);
        await shotOf("inbox", true);

        await open(page, `/p/${project}`);
        await expect(page.getByTestId("project-tool-images")).toBeVisible();
        await page.getByTestId("project-output-settings").scrollIntoViewIfNeeded();
        await shotOf("project");
      }
    });
  }
});
