import { createAgentByApi } from "./support/agents";
import { agentAsks, agentSays, chatPageOf, chatWorker, claimChatRun, openDirectByApi, sendByApi, takeInbox } from "./support/chats";
import { expect, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";

const OUT = process.env.SHOTS_DIR ?? "/tmp/shots";
const TAG = process.env.SHOTS_TAG ?? "before";
const LONG =
  "I read the plan and the three pull requests it names. The first one moves the worker's queue onto the hub, the second adds the retry policy and the third one is the web change that shows a run's progress under its message. " +
  "Two things block the release: the migration of the second pull request runs on an empty table only, and the third one has no test for a run that ends while the page is hidden.\n\n" +
  "- Fix the migration so it copies the rows the hub holds now.\n- Add the test for the hidden page.\n- Then tag the release and watch the deploy.\n\nI can do the first two now if you agree; the third needs your go-ahead.";

test("chat layout shots", async ({ page, member }) => {
  test.setTimeout(120_000);
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("lead");
  await createAgentByApi(me, project, { handle, name: "Tech lead" });
  const live = await chatWorker(me, project, uniqueName("box"), 1, handle);
  const chat = await openDirectByApi(me, handle);
  await sendByApi(me, chat.id, "Can you check the release plan and tell me what still blocks it? Keep it short.");
  const runId = (await chatPageOf(me, chat.id)).messages[0].mentions[0].run_id!;
  await claimChatRun(live, runId);
  await takeInbox(live, runId);
  await agentSays(live, runId, LONG);
  await agentAsks(live, runId, "One question before I go on.", "Fix the migration and the test now, then tag the release?");
  for (const [width, height] of [[1920, 1080], [1440, 900], [375, 812], [768, 1024]]) {
    await page.setViewportSize({ width, height });
    await open(page, `/chats/${chat.id}`);
    await expect(page.getByTestId("chat-message").first()).toBeVisible();
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${OUT}/chat-${TAG}-${width}.png` });
    await open(page, `/?agent=${handle}`);
    await expect(page.getByTestId("chat-message").first()).toBeVisible();
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${OUT}/home-${TAG}-${width}.png` });
    const sideways = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    console.log(`width ${width} sideways ${sideways}`);
  }
});
