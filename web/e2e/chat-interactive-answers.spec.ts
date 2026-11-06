import type { Locator, Page } from "@playwright/test";

import { createAgentByApi } from "./support/agents";
import { expectNoSeriousViolations } from "./support/a11y";
import { agentSays, chatPageOf, chatSession, chatWorker } from "./support/chats";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { HubAdmin, registration, uniqueName } from "./support/hub";
import { sendOutputs } from "./support/outputs";
import { open } from "./support/plans";

/**
 * Interactive answers in a chat (plan chat-interactive-answers, criteria a3 and a4): the spec is the agent's machine,
 * sending an HTML file marked interactive and the message that carries it, as `worker output --interactive` and the
 * chat run's message do. The web shows it under the message's text in a frame `sandbox="allow-scripts"`, sized by the
 * page within bounds, loaded once the message comes near the viewport; an HTML file not marked stays a chip. A button
 * in the page asks through the bridge evo-answer/1, and the question goes to the agent in the answer's thread. At 375
 * and 1440 px, light and dark, axe finds nothing serious and the page never scrolls sideways.
 */
test.skip(isDeployed, "runs a fake worker against the local stack");

const BRIDGE = "evo-answer/1";

/** An answer that speaks the bridge by itself, as one must in a restricted chat, where no helper loads. */
const ANSWER_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Plans compared</title>
<style>html,body{margin:0;background:#fff;color:#111;font:14px/1.5 system-ui,sans-serif}main{padding:12px}
button{margin:4px 4px 0 0;padding:8px 12px;border:1px solid #444;border-radius:6px;background:#fff;color:#111}</style></head>
<body><main><h1>Plans compared</h1><p id="context">waiting</p>
<button id="ask" type="button">Ask about Q3</button>
<button id="open" type="button">Open the source</button>
<button id="insecure" type="button">Open over http</button>
<button id="navigate" type="button">Go elsewhere</button>
<p id="result" aria-live="polite"></p></main>
<script>
var next = 0, waiting = {};
function call(method, params) {
  next += 1;
  var id = "t" + next;
  return new Promise(function (resolve, reject) {
    waiting[id] = { resolve: resolve, reject: reject };
    parent.postMessage({ bridge: "${BRIDGE}", id: id, method: method, params: params || {} }, "*");
  });
}
addEventListener("message", function (event) {
  var data = event.data;
  if (event.source !== parent || !data || data.bridge !== "${BRIDGE}") return;
  if (data.event === "theme") { document.getElementById("context").textContent = "theme " + data.theme; return; }
  var found = waiting[data.id];
  if (!found) return;
  delete waiting[data.id];
  if (data.error) found.reject(data.error); else found.resolve(data.result);
});
function show(promise) {
  promise.then(
    function (result) { document.getElementById("result").textContent = "ok " + JSON.stringify(result); },
    function (error) { document.getElementById("result").textContent = "refused " + error.code; });
}
call("context").then(function (context) {
  document.getElementById("context").textContent = "theme " + context.theme + ", " + context.locale;
  return call("resize", { height: 420 });
});
document.getElementById("ask").onclick = function () { show(call("sendPrompt", { text: "  Compare Q3 too  " })); };
document.getElementById("open").onclick = function () { show(call("openLink", { url: "https://example.com/source" })); };
document.getElementById("insecure").onclick = function () { show(call("openLink", { url: "http://example.com/" })); };
document.getElementById("navigate").onclick = function () { show(call("navigate", { to: "/chats" })); };
</script></body></html>`;

/** An answer that uses the hub's helper and its UI kit, served from 'self' in a chat that is not restricted. */
const KIT_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Kit answer</title>
<link rel="stylesheet" href="/v1/plugin-ui/kit/1/evo-ui.css">
<script src="/v1/plugin-ui/kit/1/evo-answer.js"></script></head>
<body><main style="padding:12px"><p id="out">waiting</p></main>
<script>evoAnswer.ready.then(function (context) { document.getElementById("out").textContent = "ready " + context.theme; });</script>
</body></html>`;

const PLAIN_PAGE = `<!doctype html><html lang="en"><head><title>Plain</title></head><body><p>Plain page</p></body></html>`;

const html = (text: string) => Buffer.from(text, "utf8");

/**
 * A chat that is not restricted, where agent Analyst answered in a thread with the two interactive answers and one
 * plain HTML file, then said each of `extra`. The stack's projects clear their hub sink at internal with no location,
 * which `telegram.restricted` reads as the top location, domestic-only: restricted, served without 'self' or the CDNs.
 * This project's hub sink names location any.
 */
async function answeredChat(me: Member, extra: string[] = []) {
  const project = me.projects[0];
  const sinks = (registration().sinks ?? []).map((sink) =>
    sink.kind === "hub" ? { ...sink, clearance: { level: "internal", location: "any" } } : sink,
  );
  await new HubAdmin().registerProject(project, { sinks });
  const handle = uniqueName("analyst");
  await createAgentByApi(me, project, { handle, name: "Analyst" });
  const live = await chatWorker(me, project, uniqueName("chat-box"), 1, handle);
  const { chat, runs } = await chatSession(me, handle, live, ["Compare the two plans"]);
  const [answer, kit] = await sendOutputs(live, runs[0], [
    { name: "compare.html", media_type: "text/html", data: html(ANSWER_PAGE), interactive: true, caption: "Plans compared" },
    { name: "kit.html", media_type: "text/html", data: html(KIT_PAGE), interactive: true },
    { name: "plain.html", media_type: "text/html", data: html(PLAIN_PAGE) },
  ]);
  await agentSays(live, runs[0], "The second plan ships two weeks sooner; the chart below compares them.");
  for (const text of extra) await agentSays(live, runs[0], text);
  const [root] = (await chatPageOf(me, chat.id)).messages;
  const thread = (await chatPageOf(me, chat.id, root.id)).messages;
  return { chat, root, thread, answer, kit, handle };
}

const carrier = (page: Page) => page.getByTestId("chat-thread").getByTestId("chat-message").filter({ hasText: "ships two weeks sooner" });
const answerOf = (message: Locator, outputId: number) => message.locator(`[data-testid="interactive-answer"][data-output-id="${outputId}"]`);

async function noSidewaysScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
}

test("an interactive answer shows in its message in a sandboxed frame sized by its page, and a plain HTML file stays a chip", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const { chat, root, answer, kit } = await answeredChat(me);
  await open(page, `/chats/${chat.id}?thread=${root.id}`);
  const message = carrier(page);
  const shown = answerOf(message, answer.id);
  await expect(shown).toHaveAttribute("data-state", "ready");
  const frame = shown.getByTestId("interactive-answer-frame");
  await expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  expect(await frame.getAttribute("sandbox")).not.toContain("allow-same-origin");
  await expect(shown).toContainText("Plans compared");
  // The page asked 420 px through resize; the web holds it.
  await expect.poll(() => frame.evaluate((element) => Math.round(element.getBoundingClientRect().height))).toBe(420);
  await expect(page.frameLocator(`[data-output-id="${answer.id}"] iframe`).locator("#context")).toContainText("theme light, en");
  // The helper from the hub's kit answers too, from 'self'.
  await expect(page.frameLocator(`[data-output-id="${kit.id}"] iframe`).locator("#out")).toHaveText("ready light");
  // The plain HTML file is a chip that opens the viewer; the answers are not chips.
  const chips = message.getByTestId("message-files").getByTestId("output-chip");
  await expect(chips).toHaveCount(1);
  await expect(chips.first()).toHaveAttribute("data-viewer", "html");
  await expect(chips.first()).toHaveAccessibleName(/plain\.html/);
});

test("a button in the answer asks the agent that wrote it, in the answer's thread, and the bridge refuses what it does not offer", async ({ page, member, context }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const { chat, root, answer } = await answeredChat(me);
  await open(page, `/chats/${chat.id}?thread=${root.id}`);
  const shown = answerOf(carrier(page), answer.id);
  await expect(shown).toHaveAttribute("data-state", "ready");
  const inside = page.frameLocator(`[data-output-id="${answer.id}"] iframe`);

  // A message posted by the page around the frame, not by the frame, is no request of the answer.
  await page.evaluate((bridge) => window.postMessage({ bridge, id: 1, method: "sendPrompt", params: { text: "forged" } }, "*"), BRIDGE);

  await inside.locator("#ask").click();
  await expect(inside.locator("#result")).toContainText("ok {\"message_id\":");
  const asked = page.getByTestId("chat-thread").getByTestId("chat-message").filter({ hasText: "Compare Q3 too" });
  await expect(asked).toHaveCount(1);
  await expect.poll(async () => {
    const thread = (await chatPageOf(me, chat.id, root.id)).messages;
    const sent = thread.find((item) => item.body === "Compare Q3 too");
    return sent ? { from: sent.from_answer, via: sent.mentions[0]?.via, run: Boolean(sent.mentions[0]?.run_id) } : null;
  }).toEqual({ from: answer.id, via: "answer", run: true });
  const thread = (await chatPageOf(me, chat.id, root.id)).messages;
  expect(thread.some((item) => item.body === "forged")).toBe(false);
  expect(thread.find((item) => item.body === "Compare Q3 too")?.mentions[0]).toMatchObject({ via: "answer" });

  // openLink opens an https URL in a new tab, with no opener; anything else is refused, as is a method it lacks.
  await context.route("https://example.com/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<title>Source</title>" }));
  const [tab] = await Promise.all([context.waitForEvent("page"), inside.locator("#open").click()]);
  await tab.waitForLoadState();
  expect(tab.url()).toBe("https://example.com/source");
  expect(await tab.evaluate(() => window.opener)).toBeNull();
  await tab.close();
  await inside.locator("#insecure").click();
  await expect(inside.locator("#result")).toHaveText("refused invalid_params");
  await inside.locator("#navigate").click();
  await expect(inside.locator("#result")).toHaveText("refused unknown_method");
});

test("the frame loads only once its message comes near the viewport", async ({ page, member }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const later = Array.from({ length: 14 }, (_, index) => `Note ${index + 1}. ${"The rollout plan needs one more review of the numbers. ".repeat(8)}`);
  const { chat, root, answer, thread } = await answeredChat(me, later);
  const last = thread.at(-1);
  if (!last) throw new Error("the thread has no messages");
  await open(page, `/chats/${chat.id}?thread=${root.id}&message=${last.id}`);
  await expect(page.getByTestId("chat-thread").getByTestId("chat-message").filter({ hasText: "Note 14." })).toBeInViewport();
  const shown = answerOf(carrier(page), answer.id);
  await expect(shown).toHaveAttribute("data-state", "idle");
  await expect(shown.getByTestId("interactive-answer-frame")).toHaveCount(0);
  await shown.scrollIntoViewIfNeeded();
  await expect(shown).toHaveAttribute("data-state", "ready");
  // The chat's own route, which serves the answer under the chat's policy, not the run's.
  await expect(shown.getByTestId("interactive-answer-frame")).toHaveAttribute("src", `/v1/chats/${chat.id}/outputs/${answer.id}/content`);
});

for (const scheme of ["light", "dark"] as const) {
  test(`a message with an interactive answer passes axe at 1440 and 375 px in ${scheme}, and the page never scrolls sideways`, async ({ page, member }) => {
    await page.emulateMedia({ colorScheme: scheme });
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const { chat, root, answer, kit } = await answeredChat(me);
    for (const [width, height] of [
      [1440, 900],
      [375, 812],
    ] as const) {
      await page.setViewportSize({ width, height });
      await open(page, `/chats/${chat.id}?thread=${root.id}`);
      const shown = answerOf(carrier(page), answer.id);
      await expect(shown).toHaveAttribute("data-state", "ready");
      await expect(page.frameLocator(`[data-output-id="${kit.id}"] iframe`).locator("#out")).toHaveText(`ready ${scheme}`);
      const kitRoot = page.frameLocator(`[data-output-id="${kit.id}"] iframe`).locator("html");
      if (scheme === "dark") await expect(kitRoot).toHaveClass(/\bdark\b/);
      else await expect(kitRoot).not.toHaveClass(/\bdark\b/);
      const box = await shown.boundingBox();
      expect(box && box.x >= 0 && box.x + box.width <= width).toBe(true);
      await noSidewaysScroll(page);
      await expectNoSeriousViolations(page, `a chat thread with an interactive answer at ${width} px in ${scheme}`);
    }
  });
}
