import { agentOf, createAgentByApi, putAgentAccountByApi } from "./support/agents";
import { chatPath, createChatByApi, openDirectByApi } from "./support/chats";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { secretValue } from "./support/secrets";
import { toast } from "./support/toast";
import { registerWorker } from "./support/workers";

/**
 * An agent's menu in a chat (web/DESIGN.md, Chats): its face or name, in a direct chat's head, in a group chat's head
 * and on its messages, opens who it is, Agent page, Workers and pools and Accounts; the last two open the agent sheet,
 * where an admin of the project limits the workers its runs go to (a pool by name too) and sees the accounts it owns,
 * and a member who only writes reads both. Pages render in English.
 */
test.skip(isDeployed, "makes agents and chats through the local stack");

test("an admin opens an agent's menu from a direct chat, limits its runs to a pool and sees its accounts", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const handle = uniqueName("reviewer");
  await createAgentByApi(me, project, { handle, name: "Code reviewer", color: "#16a34a" });
  const box = await registerWorker(me, { name: uniqueName("box"), projects: [project] });
  await putAgentAccountByApi(me, project, handle, "reviewer-openai", { env_var: "OPENAI_API_KEY", value: secretValue("menu"), workers: [box.name] });
  const chat = await openDirectByApi(me, handle);
  await open(page, chatPath(chat.id));

  // The face in the head opens the menu: who the agent is, its page, and the two settings.
  const head = page.getByTestId("chat-head");
  await expect(head.getByRole("heading", { level: 1 })).toHaveAccessibleName("Code reviewer");
  await head.getByRole("button", { name: "Open the menu of Code reviewer" }).click();
  const menu = page.getByRole("menu");
  await expect(menu).toContainText(`@${handle}`);
  await expect(menu).toContainText(project);
  await expect(menu.getByRole("menuitem")).toHaveText(["Agent page", "Workers and pools", "Accounts"]);
  await expect(menu.getByRole("menuitem", { name: "Agent page" })).toHaveAttribute("href", `/p/${project}/agents/${handle}`);
  await menu.getByRole("menuitem", { name: "Workers and pools" }).click();

  // The sheet opens on Workers and pools: any worker until the admin picks.
  const sheet = page.getByTestId("agent-sheet");
  await expect(sheet.getByRole("heading", { name: "Code reviewer" })).toBeVisible();
  await expect(sheet.getByRole("tab", { name: "Workers and pools" })).toHaveAttribute("aria-selected", "true");
  const panel = sheet.getByTestId("agent-workers");
  await expect(panel).toHaveAttribute("data-editable", "true");
  await expect(panel.getByTestId("agent-workers-save")).toBeDisabled();
  await panel.getByTestId("agent-axis-worker-allowlist").click();
  await panel.getByTestId("agent-axis-worker-typed").fill("pool:shipyard");
  await panel.getByTestId("agent-axis-worker-add").click();
  await panel.getByTestId("agent-axis-worker-typed").fill("pool:");
  await panel.getByTestId("agent-axis-worker-add").click();
  await expect(panel).toContainText("A worker's name, label: with a worker's label, or pool: with a pool's name.");
  await panel.getByTestId("agent-workers-save").click();
  await expect(toast(page, `Saved where @${handle} runs`)).toBeVisible();
  await expect.poll(async () => (await agentOf(me, project, handle)).permissions.worker).toEqual({ mode: "allowlist", allow: ["pool:shipyard"] });
  await expect(panel.getByTestId("agent-workers-save")).toBeDisabled();
  await page.screenshot({ path: test.info().outputPath("agent-sheet-workers.png") });

  // Accounts: the agent page's card, with the account it owns.
  await sheet.getByRole("tab", { name: "Accounts" }).click();
  const accounts = sheet.getByTestId("agent-accounts");
  await expect(accounts).toContainText("reviewer-openai");
  await expect(accounts.getByTestId("agent-account-add")).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("agent-sheet-accounts.png") });
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
});

test("a writer reads where a group chat's agent runs from its face in the head, and the default agent owns no accounts", async ({ page, member, admin }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const handle = uniqueName("fixer");
  const permissions = { worker: { mode: "allowlist" as const, allow: ["pool:shipyard", "label:gpu"] } };
  await createAgentByApi(me, project, { handle, name: "Fixer", color: "#2563eb", permissions });
  await admin.grant(project, me.login, "writer", "internal"); // no longer an admin: reads, changes nothing
  const chat = await createChatByApi(me, { title: uniqueName("Release"), agents: [handle, project] });
  await open(page, chatPath(chat.id));

  // A group chat's head draws its agents' faces, each a menu; Workers and pools reads the axis as chips.
  const faces = page.getByTestId("chat-head-agents");
  await faces.locator(`[data-testid="agent-menu-open"][data-handle="${handle}"]`).click();
  await page.getByRole("menu").getByRole("menuitem", { name: "Workers and pools" }).click();
  const sheet = page.getByTestId("agent-sheet");
  const panel = sheet.getByTestId("agent-workers");
  await expect(panel).toHaveAttribute("data-editable", "false");
  await expect(panel.getByTestId("agents-read-only")).toBeVisible();
  await expect(panel.getByTestId("agent-workers-summary").getByRole("listitem")).toHaveText(["pool:shipyard", "label:gpu"]);
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();

  // The project's default agent owns no accounts, and its tab says so.
  await faces.locator(`[data-testid="agent-menu-open"][data-handle="${project}"]`).click();
  await page.getByRole("menu").getByRole("menuitem", { name: "Accounts" }).click();
  await expect(sheet.getByTestId("agent-sheet-default")).toContainText("owns no accounts");
});
