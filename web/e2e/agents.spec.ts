import type { Page } from "@playwright/test";

import { agentOf, agentPath, agentsPath, createAgentByApi, PIXEL_PNG } from "./support/agents";
import { signOut } from "./support/auth";
import { authorWorker, ensureAuthorSkill } from "./support/author";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { apiOf } from "./support/memories";
import { open } from "./support/plans";
import { PLAN_RUN_PLAN, RUN_PLAN, runsOf, seedPlanRunPlan, seedRunPlan } from "./support/runs";
import { putSecretByApi, secretValue } from "./support/secrets";
import { packSkill, publishSkill } from "./support/skills";
import { toast } from "./support/toast";
import { heartbeat, registerWorker } from "./support/workers";

/**
 * The Agents pages against the real API (docs/hub.md, Agent profiles): an admin creates an agent from the hub's own
 * lists (their secret, the project's skill, a worker's label, the models it lists) with an avatar, edits it into a new
 * version and deletes it, which then reads as before; a writer only reads; and Dispatch, Run plan and New plan queue
 * their runs under the agent picked, which its page then lists. Pages render in English.
 */
test.skip(isDeployed, "creates agents through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("an admin creates an agent from the hub's own lists, with an avatar, and finds it on its page and in the list", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("reviewer");
  // What the form offers: the admin's secret, the project's skill, a worker with a label and the models it lists.
  await putSecretByApi(me, "npm-token", { kind: "env", env_var: "NPM_TOKEN", projects: [project], value: secretValue("agents") });
  await publishSkill(await apiOf(me), await packSkill("team-notes", "Notes.", "Use when writing notes"), "team-notes", project);
  const gpu = await registerWorker(me, { name: uniqueName("gpu"), projects: [project], labels: ["gpu"] });
  await heartbeat(gpu.id, { runtimes: { "claude-code": { available: true, version: "2.1.289", models: ["claude-opus-4-1"] } } });

  // The sidebar leads to the project's agents: its default agent, @<project>, every permission.
  await open(page, `/p/${project}`);
  await page.getByTestId("nav-agents").click();
  await page.waitForURL(`**${agentsPath(project)}`);
  await expect(main(page).getByRole("heading", { level: 1, name: "Agents" })).toBeVisible();
  await expect(page.getByTestId("nav-agents")).toHaveAttribute("aria-current", "page");
  const rows = main(page).getByTestId("agents-table").locator("tbody tr");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText(`@${project}`);
  await expect(rows.first().getByTestId("agent-default")).toHaveText("Default");
  await expect(rows.first().getByTestId("agent-limits")).toHaveText("Every permission");

  await main(page).getByTestId("agents-new").click();
  const dialog = page.getByTestId("agent-dialog");
  await expect(dialog.getByRole("heading", { name: "New agent" })).toBeVisible();
  await dialog.getByTestId("agent-handle-input").fill(handle);
  await dialog.getByTestId("agent-name-input").fill("Code reviewer");
  await dialog.getByTestId("agent-description-input").fill("Reads diffs and leaves notes.");
  await dialog.getByTestId("agent-instructions-input").fill("Review only; never push.");
  await dialog.getByTestId("agent-colour-16a34a").click();
  await expect(dialog.getByRole("radio", { name: "Green" })).toBeChecked();
  await dialog.getByTestId("agent-avatar-pick-input").setInputFiles({ name: "face.png", mimeType: "image/png", buffer: PIXEL_PNG });
  await expect(dialog.getByTestId("agent-avatar-field")).toContainText("face.png is uploaded once the agent is created.");

  // Each axis picked from what the hub lists; a secret the admin cannot see, typed by hand.
  const allow = async (axis: string, values: string[]) => {
    const field = dialog.getByTestId(`agent-axis-${axis}`);
    await field.getByTestId(`agent-axis-${axis}-allowlist`).click();
    await expect(field).toHaveAttribute("data-mode", "allowlist");
    for (const value of values) await field.locator(`[data-testid="agent-axis-${axis}-option"][data-value="${value}"]`).check();
  };
  await allow("tool", ["read", "edit"]);
  await allow("credential", ["npm-token"]);
  await allow("skill", ["team-notes"]);
  await allow("worker", ["label:gpu"]);
  await allow("model", ["claude-opus-4-1"]);
  await allow("runtime", ["claude-code"]);
  const credentials = dialog.getByTestId("agent-axis-credential");
  await credentials.getByTestId("agent-axis-credential-typed").fill("Shared PyPI");
  await credentials.getByTestId("agent-axis-credential-add").click();
  await expect(credentials.getByTestId("agent-field-error")).toContainText("A secret's name is lowercase letters");
  await credentials.getByTestId("agent-axis-credential-typed").fill("shared-pypi");
  await credentials.getByTestId("agent-axis-credential-add").click();
  await expect(credentials.getByTestId("agent-axis-credential-count")).toHaveText("2 allowed, of at most 200.");
  await dialog.getByTestId("agent-save").click();
  await expect(dialog).toBeHidden();

  // Its page: the agent as saved, version 2 since its avatar followed its creation.
  await page.waitForURL(`**${agentPath(project, handle)}`);
  await expect(toast(page, `Agent @${handle} created`)).toBeVisible();
  const detail = main(page).getByTestId("agent-detail");
  await expect(detail.getByRole("heading", { level: 1 })).toHaveText("Code reviewer");
  await expect(detail).toHaveAttribute("data-version", "2");
  await expect(main(page).getByTestId("agent-handle")).toHaveText(`@${handle}`);
  await expect(main(page).getByTestId("agent-avatar-card").locator("img")).toBeVisible();
  await expect(main(page).getByTestId("agent-description")).toHaveText("Reads diffs and leaves notes.");
  await expect(main(page).getByTestId("agent-instructions")).toHaveText("Review only; never push.");
  await expect(main(page).getByTestId("agent-permission-tool")).toContainText("readedit");
  await expect(main(page).getByTestId("agent-permission-worker")).toContainText("label:gpu");
  await expect(main(page).getByTestId("agent-permission-runtime")).toContainText("Claude Code");
  await expect(main(page).getByTestId("agent-permission-project")).toHaveAttribute("data-mode", "all");
  const versions = main(page).getByTestId("agent-version-row");
  await expect(versions).toHaveCount(2);
  await expect(versions.first()).toContainText("Changed the avatar.");
  await expect(versions.last()).toContainText("Created the agent.");
  await expect(main(page).getByTestId("agent-runs-empty")).toContainText("No run among the project's latest 200");

  // What the hub holds.
  const saved = await agentOf(me, project, handle);
  expect(saved).toMatchObject({ name: "Code reviewer", color: "#16a34a", version: 2, instructions: "Review only; never push." });
  expect(saved.avatar_sha256).not.toBeNull();
  expect(saved.permissions).toMatchObject({
    credential: { mode: "allowlist", allow: ["npm-token", "shared-pypi"] },
    tool: { mode: "allowlist", allow: ["read", "edit"] },
    skill: { mode: "allowlist", allow: ["team-notes"] },
    worker: { mode: "allowlist", allow: ["label:gpu"] },
    project: { mode: "all" },
    model: { mode: "allowlist", allow: ["claude-opus-4-1"] },
    runtime: { mode: "allowlist", allow: ["claude-code"] },
  });

  // Back in the list, after the default: what it limits, by axis.
  await page.getByRole("navigation", { name: "Current location" }).getByRole("link", { name: "Agents" }).click();
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(1)).toContainText(`@${handle}, Reads diffs and leaves notes.`);
  await expect(rows.nth(1).getByTestId("agent-limits")).toHaveAttribute("data-limits", "credential tool skill worker model runtime");
});

test("an admin edits and deletes an agent, which then reads as before; a writer only reads", async ({ page, member, admin, signInAs }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("docs");
  await createAgentByApi(me, project, {
    handle,
    name: "Docs writer",
    description: "Writes docs.",
    permissions: { tool: { mode: "allowlist", allow: ["read", "edit"] } },
  });

  await open(page, agentPath(project, handle));
  await main(page).getByTestId("agent-edit").click();
  const dialog = page.getByTestId("agent-dialog");
  await expect(dialog.getByRole("heading", { name: `Edit @${handle}` })).toBeVisible();
  await expect(dialog.getByTestId("agent-handle-input")).toHaveCount(0); // a handle never changes
  await expect(dialog.getByTestId("agent-save")).toBeDisabled();
  await dialog.getByTestId("agent-name-input").fill("Docs editor");
  await dialog.getByTestId("agent-axis-tool").locator('[data-value="edit"]').uncheck();
  await dialog.getByTestId("agent-save").click();
  await expect(dialog).toBeHidden();
  await expect(toast(page, `@${handle} saved as version 2`)).toBeVisible();
  await expect(main(page).getByTestId("agent-detail")).toHaveAttribute("data-version", "2");
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Docs editor");
  await expect(main(page).getByTestId("agent-version-row").first()).toContainText("Changed the name and tools.");
  expect((await agentOf(me, project, handle)).permissions.tool).toMatchObject({ mode: "allowlist", allow: ["read"] });

  // Deleted, it takes no new run and leaves the list; one facet away it reads as before.
  await main(page).getByTestId("agent-delete").click();
  const confirm = page.getByTestId("agent-delete-dialog");
  await expect(confirm).toContainText(`@${handle} stays taken`);
  await confirm.getByTestId("agent-delete-dialog-confirm").click();
  await page.waitForURL(`**${agentsPath(project)}`);
  await expect(toast(page, `Agent @${handle} deleted`)).toBeVisible();
  const rows = main(page).getByTestId("agents-table").locator("tbody tr");
  await expect(rows).toHaveCount(1);
  await main(page).getByTestId("agents-facet").getByRole("button", { name: /^Deleted/ }).click();
  await expect(page).toHaveURL(/\?show=deleted$/);
  await expect(rows).toHaveCount(1);
  await expect(rows.first().getByTestId("agent-deleted")).toHaveText("Deleted");
  await rows.first().getByRole("link", { name: "Docs editor" }).click();
  await expect(main(page).getByTestId("agent-deleted-note")).toContainText("it takes no new run");
  await expect(main(page).getByTestId("agent-edit")).toHaveCount(0);
  expect((await agentOf(me, project, handle)).deleted_at).not.toBeNull();

  // A writer of the project reads its agents, and changes none.
  const writer = newAccount("writer");
  await admin.grant(project, writer.login, "writer", "internal");
  await signOut(page); // of the hub and the fake GitHub
  await signInAs(writer);
  await open(page, agentsPath(project));
  await expect(main(page).getByTestId("agents-read-only")).toContainText("only an admin of the project creates and changes them");
  await expect(main(page).getByTestId("agents-new")).toHaveCount(0);
  await open(page, agentPath(project, project));
  await expect(main(page).getByTestId("agent-default")).toHaveText("Default");
  await expect(main(page).getByTestId("agent-instructions")).toHaveText("None: the default agent never has instructions.");
  await expect(main(page).getByTestId("agent-edit")).toHaveCount(0);
  await expect(main(page).getByTestId("agent-avatar-pick")).toHaveCount(0);
});

test("Dispatch, Run plan and New plan queue their runs under the agent picked, and its page lists them", async ({ page, member }) => {
  test.setTimeout(120_000);
  await ensureAuthorSkill();
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await seedPlanRunPlan(me, project);
  const live = await authorWorker(me, project, uniqueName("desk"));
  const handle = uniqueName("runner");
  await createAgentByApi(me, project, {
    handle,
    name: "Runner",
    permissions: { runtime: { mode: "allowlist", allow: ["claude-code"] }, worker: { mode: "allowlist", allow: [live.worker.name] } },
  });

  // Dispatch: the default agent first; the agent picked says what it limits, and refuses a runtime it leaves out.
  await open(page, `/p/${project}/runs`);
  await main(page).getByTestId("runs-empty-dispatch").click();
  const dispatch = page.getByTestId("dispatch-dialog");
  await dispatch.getByTestId("dispatch-plan").selectOption(RUN_PLAN);
  await dispatch.getByTestId("dispatch-step-2").click();
  const pick = dispatch.getByTestId("dispatch-agent-select");
  await expect(pick).toHaveValue(project);
  await pick.selectOption(handle);
  await expect(dispatch.getByTestId("dispatch-agent-hint")).toHaveText(`It limits workers to ${live.worker.name} and runtimes to Claude Code.`);
  await dispatch.getByRole("radio", { name: /^Codex CLI/ }).check();
  await expect(dispatch.getByTestId("dispatch-agent-problem")).toHaveText(`@${handle} does not allow Codex CLI: it allows Claude Code.`);
  await expect(dispatch.getByTestId("dispatch-submit")).toBeDisabled();
  await dispatch.getByRole("radio", { name: /^Claude Code/ }).check();
  await expect(dispatch.getByTestId("dispatch-outlook")).toContainText(live.worker.name);
  await dispatch.getByTestId("dispatch-submit").click();
  await expect(dispatch).toBeHidden();

  // Run plan.
  await open(page, `/p/${project}/plans/${PLAN_RUN_PLAN}`);
  await main(page).getByTestId("run-plan").click();
  const planRun = page.getByTestId("plan-run-dialog");
  await planRun.getByTestId("dispatch-agent-select").selectOption(handle);
  await planRun.getByRole("radio", { name: /^Claude Code/ }).check();
  await planRun.getByTestId("plan-run-submit").click();
  await expect(planRun).toBeHidden();

  // New plan: an author run, always on Claude Code and pinned to a worker the agent allows.
  await open(page, `/p/${project}/plans`);
  await main(page).getByTestId("new-plan").click();
  const author = page.getByTestId("author-run-dialog");
  await author.getByTestId("author-run-request").fill("A plan for the agents of this project.");
  await author.getByTestId("dispatch-agent-select").selectOption(handle);
  await expect(author.getByTestId("author-run-worker-select")).toHaveValue(String(live.worker.id));
  await author.getByTestId("author-run-submit").click();
  await expect(author).toBeHidden();

  // The hub holds the three runs under the agent, at its version, and the agent's page lists them.
  const runs = await runsOf(me, project);
  expect(runs.map((run) => run.kind).sort()).toEqual(["author", "plan", "step"]);
  for (const run of runs) expect(run).toMatchObject({ profile: handle, profile_version: 1, requested_runtime: "claude-code" });
  await open(page, agentPath(project, handle));
  await expect(main(page).getByTestId("agent-runs-table").locator("tbody tr")).toHaveCount(3);
});
