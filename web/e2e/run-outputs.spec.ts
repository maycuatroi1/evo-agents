import type { Locator, Page } from "@playwright/test";

import { createAgentByApi } from "./support/agents";
import { expectNoSeriousViolations } from "./support/a11y";
import { signOut } from "./support/auth";
import { agentSays, chatPageOf, chatSession, chatWorker } from "./support/chats";
import { API_URL } from "./support/env";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { bearerClient, machineToken, newAccount, uniqueName } from "./support/hub";
import { localEvents, registerLocal } from "./support/local";
import { filesOfEachKind, type OutputFile, outputLink, pageShot, sendLocalOutputs, sendOutputs } from "./support/outputs";
import { open } from "./support/plans";
import {
  askDecision,
  claimRun,
  dispatch,
  liveWorker,
  planRunUnderway,
  runPath,
  seedPlanRunPlan,
  seedRunPlan,
  sendEvents,
  startRun,
  type WorkerEvent,
} from "./support/runs";
import { toast } from "./support/toast";

/**
 * The files a run's agent made, on the web (plan run-outputs, criteria a3 to a7): the spec is the machine, sending a
 * run's outputs through the hub's routes as a worker's daemon and a local session's hooks do (e2e/support/outputs.ts),
 * with the tool calls they came from in the run's log. The run page lists them in its Outputs tab and under their calls
 * in the Trace, and its viewer shows each kind: an image zoomed and moved through with the keys, a video that seeks, a
 * PDF, text, Markdown, JSON, CSV and code, an HTML page in a sandbox that reaches neither the hub nor the cookies, and
 * a download for the rest. The run's dispatcher and the project's admins delete one; other readers do not, and people
 * without the run get no bytes. A question, a notice and a chat message carry their files the same way. At 375 and
 * 1440 px, light and dark, axe finds nothing serious and the page never scrolls sideways.
 */
test.skip(isDeployed, "runs a fake worker against the local stack");

const main = (page: Page) => page.locator("#main");
const viewerOf = (page: Page) => page.getByTestId("output-viewer");
const tile = (page: Page, name: string) => main(page).getByTestId("output-tile").filter({ hasText: name });

const READ = "read-home";
const SEND = "send-report";
const HOME: OutputFile = { name: "home.png", media_type: "image/png", data: pageShot(1280, 800, [17, 113, 61]), source: "tool", tool_call_id: READ };

/** A run of step 2 that `me` dispatched, taken by their worker, with the agent started. */
async function runningRun(me: Member) {
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("laptop"));
  const [run] = await dispatch(me, project, ["2"]);
  expect((await claimRun(live))?.id).toBe(run.id);
  await startRun(live, run.id);
  return { project, live, run };
}

/** The agent reads a screenshot (its image taken out as a tool output) and sends a report with `worker output`. */
function calls(): WorkerEvent[] {
  return [
    { kind: "tool_call", body: { toolCallId: READ, title: "Read", kind: "read", rawInput: { file_path: "/tmp/shots/home.png" }, status: "in_progress" } },
    { kind: "tool_call_update", body: { toolCallId: READ, status: "completed", content: [{ type: "content", content: { type: "text", text: "Read image" } }, outputLink(HOME)] } },
    { kind: "tool_call", body: { toolCallId: SEND, title: "Bash", kind: "execute", rawInput: { command: 'evo-agents worker output report.md --caption "What the checkout does now"' }, status: "in_progress" } },
    { kind: "tool_call_update", body: { toolCallId: SEND, status: "completed", content: [{ type: "content", content: { type: "text", text: "queued 1 file" } }] } },
  ];
}

/**
 * A run with a file of each kind, eleven in all: the screenshot under its Read call and the report under its `worker
 * output` call, recorded first, then the nine others, as a run's output folder sends them.
 */
async function runWithFiles(me: Member) {
  const setup = await runningRun(me);
  await sendEvents(setup.live, setup.run.id, calls());
  const files = filesOfEachKind();
  const report = { ...files.find((file) => file.name === "report.md")!, caption: "What the checkout does now" };
  const first = await sendOutputs(setup.live, setup.run.id, [HOME, report]);
  const rest = await sendOutputs(setup.live, setup.run.id, files.filter((file) => file.name !== "report.md"));
  return { ...setup, outputs: [...first, ...rest.filter((output) => !first.some((held) => held.id === output.id))] };
}

async function openOutputs(page: Page, project: string, runId: number) {
  await open(page, runPath(project, runId));
  await main(page).getByTestId("run-tab-outputs").click();
  await expect(main(page).getByTestId("run-outputs-grid")).toBeVisible();
}

async function openTile(page: Page, name: string): Promise<Locator> {
  await tile(page, name).click();
  const viewer = viewerOf(page);
  await expect(viewer.getByTestId("output-viewer-name")).toHaveText(name.split("/").pop() ?? name);
  return viewer;
}

async function noSidewaysScroll(page: Page) {
  const width = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(width).toBeLessThanOrEqual(0);
}

test("a run's files show in its Outputs tab and under their calls in the Trace, and each opens in the viewer as its kind", async ({ page, member }) => {
  // What the page's own policy refuses (a frame, an image, a video of the hub's origin); the probe page's refusals of its
  // own calls, under the sandbox the hub serves it with, are the point and are checked below.
  const refused: string[] = [];
  page.on("console", (message) => {
    const own = !message.location().url.includes("/outputs/") && !message.text().includes("connect-src 'none'");
    if (own && /Refused|Content Security Policy/i.test(message.text())) refused.push(message.text());
  });
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, run } = await runWithFiles(me);
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, runPath(project, run.id));

  // The Trace: the screenshot under its Read call, the report under the call that sent it, the rest under the hub's line.
  const trace = main(page).getByTestId("trace");
  await expect(trace.getByTestId("trace-group").filter({ hasText: "Explored" }).getByTestId("trace-group-files").getByTestId("output-chip")).toHaveAccessibleName(/^Open home\.png, Image/);
  const hub = trace.getByTestId("trace-group").filter({ has: page.getByTestId("trace-group-files").filter({ hasText: "report.md" }) });
  await expect(hub).toHaveCount(1);
  await hub.getByTestId("trace-group-toggle").click();
  await expect(hub.getByTestId("trace-tool-arg")).toHaveText("report.md"); // the file it sent, not the command's path
  // The hub's line of the first two is left out, as their calls show them; that of the nine others shows them.
  await expect(trace.getByTestId("trace-recorded-files").getByTestId("output-chip").or(trace.getByTestId("output-strip-more"))).toHaveCount(9);
  await expect(trace).not.toContainText("2 output(s) recorded");

  // The Outputs tab: its count, every file newest first, chips by source.
  await expect(main(page).getByTestId("run-tab-outputs-count")).toHaveText("11");
  await main(page).getByTestId("run-tab-outputs").click();
  await expect(page).toHaveURL(/\?view=outputs$/);
  await expect(main(page).getByTestId("output-tile")).toHaveCount(11);
  await expect(main(page).getByTestId("run-outputs-summary")).toContainText("11 files");
  const facets = main(page).getByTestId("run-outputs-facets");
  await facets.getByRole("button", { name: /Output folder/ }).click();
  await expect(main(page).getByTestId("output-tile")).toHaveCount(1);
  await facets.getByRole("button", { name: /Tools/ }).click();
  await expect(main(page).getByTestId("output-tile")).toHaveText([/home\.png/]);
  await facets.getByRole("button", { name: /^All/ }).click();

  // An image: drawn from the hub's own origin, zoomed with the buttons and the keys, the arrows move on.
  let viewer = await openTile(page, "home.png");
  await expect(viewer.getByTestId("output-viewer-meta")).toContainText("From a tool's result");
  await expect.poll(() => viewer.getByTestId("output-image").evaluate((image: HTMLImageElement) => image.naturalWidth)).toBe(1280);
  await viewer.getByTestId("output-viewer-zoom-in").click();
  await expect(viewer.getByTestId("output-image-box")).toHaveAttribute("data-zoom", "1.5");
  await page.keyboard.press("0");
  await expect(viewer.getByTestId("output-image-box")).toHaveAttribute("data-zoom", "1");
  await page.keyboard.press("ArrowLeft");
  await expect(viewer.getByTestId("output-viewer-name")).toHaveText("report.md");
  await page.keyboard.press("ArrowRight");
  await expect(viewer.getByTestId("output-viewer-name")).toHaveText("home.png");
  await page.keyboard.press("ArrowRight"); // the oldest wraps round to the newest
  await expect(viewer.getByTestId("output-viewer-name")).toHaveText("bundle.zip");
  await page.keyboard.press("Escape");
  await expect(viewer).toHaveCount(0);
  await expect(tile(page, "home.png")).toBeFocused();

  // A video, which seeks: the hub answers byte ranges.
  viewer = await openTile(page, "checkout.webm");
  await expect(viewer.getByTestId("output-viewer-caption")).toHaveText("The checkout, recorded with the fix");
  const video = viewer.getByTestId("output-video");
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.duration)).toBeGreaterThan(2.5);
  const seeked = await video.evaluate(
    (element: HTMLVideoElement) =>
      new Promise<number>((resolve) => {
        element.addEventListener("seeked", () => resolve(element.currentTime), { once: true });
        element.currentTime = 2;
      }),
  );
  expect(seeked).toBeGreaterThan(1.5);
  await page.keyboard.press("Escape");

  // Audio, a PDF in a frame (the hub serves it inline, its CSP lets only the hub's pages frame it).
  viewer = await openTile(page, "chime.wav");
  await expect(viewer.getByTestId("output-audio")).toBeVisible();
  await page.keyboard.press("Escape");
  const pdf = page.waitForResponse((response) => response.url().includes("/content") && response.request().resourceType() === "document");
  viewer = await openTile(page, "release-notes.pdf");
  const pdfAnswer = await pdf;
  expect(pdfAnswer.status()).toBe(200);
  expect(pdfAnswer.headers()["content-type"]).toBe("application/pdf");
  expect(pdfAnswer.headers()["content-security-policy"]).toBe("frame-ancestors 'self'");
  await expect(viewer.getByTestId("output-pdf")).toBeVisible();
  await expect(viewer.getByTestId("output-pdf-tab")).toHaveAttribute("target", "_blank");
  await page.keyboard.press("Escape");

  // Text of every kind, read from the hub as text/plain and drawn as what it holds.
  viewer = await openTile(page, "report.md");
  await expect(viewer.getByRole("heading", { name: "Checkout findings" })).toBeVisible();
  await expect(viewer.getByTestId("output-viewer-caption")).toHaveText("What the checkout does now");
  await page.keyboard.press("Escape");
  viewer = await openTile(page, "results.json");
  await expect(viewer.getByTestId("output-pre")).toContainText('"passed": 42');
  await page.keyboard.press("Escape");
  viewer = await openTile(page, "timings.csv");
  await expect(viewer.getByTestId("output-csv").getByRole("cell", { name: "cart" })).toBeVisible();
  await page.keyboard.press("Escape");
  viewer = await openTile(page, "fix_total.py");
  await expect(viewer.getByTestId("output-pre")).toContainText("def total(items):");
  await page.keyboard.press("Escape");

  // HTML runs in a sandbox: its script reads no cookie and reaches no route of the hub.
  viewer = await openTile(page, "probe.html");
  await expect(viewer.getByTestId("output-html")).toHaveAttribute("sandbox", "allow-scripts");
  const frame = page.frameLocator('[data-testid="output-html"]');
  await expect(frame.getByRole("heading", { name: "Probe of northwind" })).toBeVisible();
  await expect(frame.locator("#cookie")).toHaveText("cookie blocked");
  await expect(frame.locator("#api")).toHaveText("api blocked");
  await page.keyboard.press("Escape");

  // Anything else is a download, under its own name.
  viewer = await openTile(page, "bundle.zip");
  const link = viewer.getByTestId("output-no-preview").getByRole("link");
  const href = await link.getAttribute("href");
  expect(href).toMatch(/\/content\?download=true$/);
  const download = await page.request.get(href!);
  expect(download.headers()["content-disposition"]).toContain('attachment; filename="bundle.zip"');
  await page.keyboard.press("Escape");

  // Show in trace: the Trace tab, the screenshot's call open and focused.
  viewer = await openTile(page, "home.png");
  await viewer.getByTestId("output-show-in-trace").click();
  await expect(viewerOf(page)).toHaveCount(0);
  await expect(main(page).getByTestId("run-tab-trace")).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(`[data-tool-call-id="${READ}"] > summary`)).toBeFocused();
  expect(refused).toEqual([]);
});

test("the owner deletes a file, which leaves the tab, the trace and the hub", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, run, outputs } = await runWithFiles(me);
  await openOutputs(page, project, run.id);
  const viewer = await openTile(page, "bundle.zip");
  await viewer.getByTestId("output-delete").click();
  const confirm = page.getByTestId("output-delete-confirm");
  await expect(confirm).toContainText("Delete bundle.zip?");
  await confirm.getByRole("button", { name: "Delete file" }).click();
  await expect(toast(page, "Deleted bundle.zip")).toBeVisible();
  // The viewer goes on to the file beside it.
  await expect(viewer.getByTestId("output-viewer-name")).not.toHaveText("bundle.zip");
  await page.keyboard.press("Escape");
  await expect(main(page).getByTestId("output-tile")).toHaveCount(10);
  await expect(main(page).getByTestId("run-tab-outputs-count")).toHaveText("10");
  const gone = outputs.find((output) => output.name === "bundle.zip")!;
  const answer = await page.request.get(gone.url);
  expect(answer.status()).toBe(404);
});

test("a file the agent sends while the run goes on shows without a reload", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendOutputs(live, run.id, [{ name: "first.png", media_type: "image/png", data: pageShot(640, 400, [97, 54, 194]) }]);
  await openOutputs(page, project, run.id);
  await expect(main(page).getByTestId("output-tile")).toHaveCount(1);
  await sendOutputs(live, run.id, [{ name: "second.png", media_type: "image/png", data: pageShot(640, 400, [188, 120, 0]) }]);
  await expect(main(page).getByTestId("output-tile")).toHaveCount(2, { timeout: 15_000 });
  await expect(main(page).getByTestId("output-tile").first()).toContainText("second.png");
  await expect(main(page).getByTestId("run-tab-outputs-count")).toHaveText("2");
});

for (const scheme of ["light", "dark"] as const) {
  test(`the Outputs tab and the viewer pass axe at 1440 and 375 px in ${scheme}, and the page never scrolls sideways`, async ({ page, member }) => {
    await page.emulateMedia({ colorScheme: scheme });
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const { project, run } = await runWithFiles(me);
    for (const [width, height] of [
      [1440, 900],
      [375, 812],
    ] as const) {
      await page.setViewportSize({ width, height });
      await open(page, `${runPath(project, run.id)}?view=outputs`);
      await expect(main(page).getByTestId("run-outputs-grid")).toBeVisible();
      await noSidewaysScroll(page);
      await expectNoSeriousViolations(page, `the Outputs tab at ${width} px in ${scheme}`);
      for (const name of ["home.png", "report.md", "probe.html"]) {
        await openTile(page, name);
        await noSidewaysScroll(page);
        await expectNoSeriousViolations(page, `the viewer of ${name} at ${width} px in ${scheme}`);
        await page.keyboard.press("Escape");
        await expect(viewerOf(page)).toHaveCount(0);
      }
    }
  });
}

test("on a phone the run page previews its files, opens them on the session's screen, and a swipe moves through them", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, run } = await runWithFiles(me);
  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, runPath(project, run.id));
  const preview = main(page).getByTestId("run-outputs-preview");
  await expect(preview.getByTestId("output-chip")).toHaveCount(6);
  await preview.getByTestId("run-outputs-preview-open").click();
  await expect(main(page).getByTestId("run-detail")).toHaveAttribute("data-screen", "session");
  await expect(main(page).getByTestId("output-tile")).toHaveCount(11);
  const viewer = await openTile(page, "home.png");
  const box = (await viewer.getByTestId("output-viewer-body").boundingBox())!;
  expect(box.width).toBe(375); // the viewer is the phone's whole width
  const image = viewer.getByTestId("output-image-box");
  const y = box.y + box.height / 2;
  await image.dispatchEvent("pointerdown", { pointerType: "touch", clientX: 300, clientY: y, isPrimary: true, bubbles: true });
  await image.dispatchEvent("pointerup", { pointerType: "touch", clientX: 120, clientY: y + 10, isPrimary: true, bubbles: true });
  await expect(viewer.getByTestId("output-viewer-name")).not.toHaveText("home.png");
  await noSidewaysScroll(page);
  await page.keyboard.press("Escape");
  // Back leaves the screen for the page, as with the trace's.
  await page.goBack();
  await expect(main(page).getByTestId("run-outputs-preview")).toBeVisible();
});

test("another reader of the run sees its files but cannot delete one, an admin can, and nobody else gets the bytes", async ({
  page,
  member,
  admin,
  signInAs,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, run, outputs } = await runWithFiles(me);
  const reader = newAccount("reader");
  await admin.grant(project, reader.login, "reader", "internal");
  await signOut(page);
  await signInAs(reader);
  await openOutputs(page, project, run.id);
  const viewer = await openTile(page, "home.png");
  await expect(viewer.getByTestId("output-viewer-download")).toBeVisible();
  await expect(viewer.getByTestId("output-delete")).toHaveCount(0);
  await page.keyboard.press("Escape");
  const api = bearerClient(await machineToken(reader));
  const refused = await api.DELETE("/v1/projects/{project}/runs/{run_id}/outputs/{output_id}", {
    params: { path: { project, run_id: run.id, output_id: outputs[0].id } },
  });
  expect(refused.response.status).toBe(403);

  // Someone without a grant on the project gets the 404 of a file that does not exist; the reader gets its bytes.
  const read = await fetch(`${API_URL}${outputs[0].url}`, { headers: { authorization: `Bearer ${await machineToken(reader)}` } });
  expect(read.status).toBe(200);
  expect(read.headers.get("x-content-type-options")).toBe("nosniff");
  const outsider = await fetch(`${API_URL}${outputs[0].url}`, { headers: { authorization: `Bearer ${await machineToken(newAccount("stranger"))}` } });
  expect(outsider.status).toBe(404);

  // An admin of the project deletes it.
  const boss = newAccount("boss");
  await admin.grant(project, boss.login, "admin", "internal");
  await signOut(page);
  await signInAs(boss);
  await openOutputs(page, project, run.id);
  await openTile(page, "probe.html");
  await viewerOf(page).getByTestId("output-delete").click();
  await page.getByTestId("output-delete-confirm").getByRole("button", { name: "Delete file" }).click();
  await expect(toast(page, "Deleted probe.html")).toBeVisible();
});

test("a question carries its screenshot to the run page and the Inbox, and a notice of the run's files shows them", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { live, run } = await planRunUnderway(me, project, uniqueName("asker"));
  const [shot] = await sendOutputs(live, run.id, [
    { name: "staging.png", media_type: "image/png", data: pageShot(800, 500, [179, 38, 30]), caption: "Staging, before the deploy" },
    { name: "build.log", media_type: "text/plain", data: Buffer.from("built northwind 2.4.0 in 41 s\n") },
  ]);
  const decision = await askDecision(live, run.id, "Deploy this build to staging?", "3", { output_ids: [shot.id] });

  // The run page's card of the question.
  await open(page, runPath(project, run.id));
  const card = main(page).getByTestId("decision").filter({ hasText: "Deploy this build to staging?" });
  await card.getByTestId("decision-files").getByTestId("output-chip").click();
  await expect(viewerOf(page).getByTestId("output-viewer-name")).toHaveText("staging.png");
  await expect.poll(() => viewerOf(page).getByTestId("output-image").evaluate((image: HTMLImageElement) => image.naturalWidth)).toBe(800);
  await page.keyboard.press("Escape");

  // The Inbox: the question's row and its sheet, and the notice of the run's files.
  await open(page, "/inbox");
  const asked = main(page).getByTestId("inbox-waiting").getByTestId("notification").filter({ hasText: "Deploy this build to staging?" });
  await expect(asked.getByTestId("notification-files").getByTestId("output-chip")).toHaveCount(1);
  const notice = main(page).getByTestId("notification").filter({ has: page.getByTestId("notice-kind").filter({ hasText: "Files from a run" }) });
  // The file the question carries is told with the question; the notice tells the other one.
  await expect(notice.getByTestId("notification-files").getByTestId("output-chip")).toHaveAccessibleName(/^Open build\.log/);
  await open(page, `/inbox?decision=${decision}`);
  const sheet = page.getByTestId("decision-sheet");
  await sheet.getByTestId("decision-files").getByTestId("output-chip").click();
  await expect(viewerOf(page).getByTestId("output-viewer-caption")).toHaveText("Staging, before the deploy");
  await page.keyboard.press("Escape");
  await expect(viewerOf(page)).toHaveCount(0);
  await expect(sheet).toBeVisible(); // Esc closed the viewer only

  // The member turns the notices of files off; questions still come.
  await open(page, "/inbox");
  await main(page).getByTestId("inbox-notice-settings").click();
  const settings = page.getByTestId("notice-settings-dialog");
  const toggle = settings.getByTestId("notice-setting-run_outputs-switch");
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(toast(page, "You no longer get the notice: Files from your runs")).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  const api = bearerClient(await machineToken(me));
  const { data } = await api.GET("/v1/me/notification-settings");
  expect(data?.notices).toEqual([{ kind: "run_outputs", on: false }]);
});

test("an agent's message in a group chat carries the files its run sent", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("painter");
  await createAgentByApi(me, project, { handle, name: "Painter" });
  const live = await chatWorker(me, project, uniqueName("chat-box"));
  const { chat, runs } = await chatSession(me, handle, live, ["Draw the release chart"]);
  await sendOutputs(live, runs[0], [{ name: "chart.png", media_type: "image/png", data: pageShot(720, 480, [15, 140, 128]) }]);
  await agentSays(live, runs[0], "Here is the chart.");
  // The agent answers in the thread of the mention.
  const [root] = (await chatPageOf(me, chat.id)).messages;
  await open(page, `/chats/${chat.id}?thread=${root.id}`);
  const files = page.getByTestId("chat-message").filter({ hasText: "Here is the chart." }).getByTestId("message-files");
  await files.getByTestId("output-chip").first().click();
  await expect(viewerOf(page).getByTestId("output-viewer-name")).toHaveText("chart.png");
  await expect.poll(() => viewerOf(page).getByTestId("output-image").evaluate((image: HTMLImageElement) => image.naturalWidth)).toBe(720);
});

test("an admin of the project switches the images of tools' results off, and a reader sees the switch only", async ({ page, member, admin, signInAs }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await open(page, `/p/${project}`);
  const card = main(page).getByTestId("project-output-settings");
  await expect(card.getByTestId("project-output-limits")).toContainText("500 files");
  const toggle = card.getByTestId("project-tool-images");
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(toast(page, "Images of tools' results are no longer kept")).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-checked", "false");

  const reader = newAccount("reader");
  await admin.grant(project, reader.login, "reader", "internal");
  await signOut(page);
  await signInAs(reader);
  await open(page, `/p/${project}`);
  await expect(main(page).getByTestId("project-tool-images")).toBeDisabled();
  await expect(main(page).getByTestId("project-tool-images")).toHaveAttribute("aria-checked", "false");
});

test("a local session's SendUserFile and screenshot show as files in its trace, never as paths", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const { run } = await registerLocal(me, project, "0199a3c1-0000-7000-8000-0000000000b7");
  const shot: OutputFile = { name: "mcp__browser__screenshot.jpg", media_type: "image/png", data: pageShot(900, 560, [47, 75, 216], true), source: "tool", tool_call_id: "toolu_shot" };
  const compare: OutputFile = { name: "compare.png", media_type: "image/png", data: pageShot(900, 560, [188, 120, 0]), tool_call_id: "toolu_send", caption: "Before and after" };
  await localEvents(me, project, run.id, [
    { kind: "tool_call", body: { toolCallId: "toolu_shot", title: "mcp__Claude_Browser__browser_batch", kind: "other", rawInput: { actions: [{ name: "computer" }] }, status: "in_progress" } },
    { kind: "tool_call_update", body: { toolCallId: "toolu_shot", status: "completed", content: [outputLink({ ...shot, name: "browser screenshot" })] } },
    { kind: "tool_call", body: { toolCallId: "toolu_send", title: "SendUserFile", kind: "other", rawInput: { files: ["/Users/binh/work/compare.png"], caption: "Before and after" }, status: "in_progress" } },
    { kind: "tool_call_update", body: { toolCallId: "toolu_send", status: "completed", content: [{ type: "content", content: { type: "text", text: '{"files":["/Users/binh/work/compare.png"]}' } }] } },
  ]);
  await sendLocalOutputs(me, project, run.id, [shot, compare]);
  await open(page, runPath(project, run.id));
  const trace = main(page).getByTestId("trace");
  await expect(trace.getByTestId("trace-group-files").getByTestId("output-chip")).toHaveCount(2);
  await trace.getByTestId("trace-group-toggle").first().click();
  const send = trace.locator('[data-tool-call-id="toolu_send"]');
  await expect(send.getByTestId("trace-tool-arg")).toHaveText("compare.png");
  await send.locator("summary").click();
  await expect(send.getByTestId("trace-tool-files").getByTestId("output-chip")).toHaveCount(1);
  await expect(trace).not.toContainText("/Users/binh/work/compare.png");
});
