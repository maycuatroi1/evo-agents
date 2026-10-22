import type { Locator, Page } from "@playwright/test";

import type { components } from "../src/lib/api/schema";

import { signInToFakeGitHub, signOut } from "./support/auth";
import { WEB_ORIGIN } from "./support/env";
import { expect, isDeployed, test } from "./support/fixtures";
import { type Account, ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import { toast } from "./support/toast";

/**
 * An invite link from end to end, against the local stack and its fake GitHub: the public page a visitor opens, GitHub's
 * sign-in coming back to it, the one-button accept and the Get started list of each kind of link; a link bound to one
 * login, which keeps its project and role from anyone else; and the admin area, where a link is made, shown once and
 * revoked. Pages render in English.
 */
test.skip(isDeployed, "makes invite links and accounts on the local stack");

type InviteCreated = components["schemas"]["InviteCreated"];

/** The page of a link the hub handed out: its EVO_HUB_PUBLIC_URL is this web's origin, so the link opens here. */
function pathOf(created: InviteCreated): string {
  expect(created.url).toBe(`${WEB_ORIGIN}/invite/${created.token}`);
  return `/invite/${created.token}`;
}

/** The command a line of the Get started list copies. */
function command(list: Locator, testId: string): Locator {
  return list.getByTestId(testId).locator("code");
}

/** GitHub's sign-in from the invite page, as `account`, back on the same page, which now knows who signed in. */
async function signInFromInvite(page: Page, account: Account, path: string): Promise<void> {
  await signInToFakeGitHub(page.context(), account);
  await page.getByTestId("invite-sign-in").click();
  await page.waitForURL((url) => url.pathname === path);
  await expect(page.getByTestId("invite-summary")).not.toHaveAttribute("data-state", "signed-out");
}

async function accept(page: Page): Promise<Locator> {
  await page.getByTestId("invite-accept").click();
  await expect(page.getByTestId("invite-accept")).toHaveCount(0);
  const list = page.getByTestId("invite-get-started");
  await expect(list).toBeVisible();
  return list;
}

test("a join link: the visitor sees who invites them where, signs in with GitHub, accepts and gets started", async ({ page, admin }) => {
  const project = uniqueName("invited");
  await admin.registerProject(project);
  const path = pathOf(await admin.invite({ kind: "join", project, role: "writer", max_level: "internal", days: 7 }));
  const account = newAccount("joiner");

  await page.goto(path);
  await expect(page).toHaveURL(`${WEB_ORIGIN}${path}`); // public: no detour through /login
  const summary = page.getByTestId("invite-summary");
  await expect(summary).toHaveAttribute("data-state", "signed-out");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(`${ADMIN_ACCOUNT.login} invites you to Evo Agent Hub`);
  await expect(page.getByTestId("invite-inviter")).toHaveText(ADMIN_ACCOUNT.login);
  await expect(page.getByTestId("invite-project")).toHaveText(project);
  await expect(page.getByTestId("invite-role")).toContainText("Writer");
  await expect(page.getByTestId("invite-sign-in")).toHaveText("Sign in with GitHub");
  // A link is a credential: never indexed, and its title and meta name no project.
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute("content", /noindex/);
  expect(await page.evaluate(() => document.head.innerHTML)).not.toContain(project);

  await signInFromInvite(page, account, path);
  await expect(summary).toHaveAttribute("data-state", "for-viewer");
  await expect(page.getByTestId("invite-viewer")).toContainText(account.login);
  const list = await accept(page);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("You accepted the invitation");
  await expect(page.getByTestId("invite-accepted")).toContainText(`You now work on project ${project} with the Writer role.`);

  await expect(command(list, "invite-command-install")).toHaveText("uv tool install 'evo-ak[graphify,worker]'");
  await expect(command(list, "invite-command-login")).toHaveText(`evo-agents hub login --url ${WEB_ORIGIN}`);
  await expect(command(list, "invite-command-claude-1")).toHaveText("claude plugin marketplace add https://github.com/maycuatroi1/evo-agents");
  await expect(command(list, "invite-command-service")).toHaveText("evo-agents worker service install");
  await expect(list.getByTestId("invite-step-project")).toHaveCount(0); // a join link registers nothing
  await expect(list.getByTestId("invite-credentials")).toContainText("Claude Code, Codex or opencode login");
  await expect(list.getByTestId("invite-credentials")).toContainText("git credentials");
  // Every command has its copy button.
  const lines = list.getByRole("group");
  expect(await lines.count()).toBeGreaterThanOrEqual(6);
  for (const line of await lines.all()) await expect(line.getByRole("button", { name: /^Copy the / })).toBeVisible();

  // Pairing a worker opens the registration dialog in place, with its pairing code.
  await list.getByTestId("invite-pair-worker").click();
  const dialog = page.getByTestId("register-dialog");
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // The grant is real: Run plan opens the project's plans, and the link, opened again, stays accepted.
  await list.getByTestId("invite-run-plans").click();
  await expect(page).toHaveURL(`${WEB_ORIGIN}/p/${project}/plans`);
  await expect(page.locator("#main").getByTestId("state-forbidden")).toHaveCount(0);
  await page.goto(path);
  await expect(page.getByTestId("invite-accepted")).toHaveAttribute("data-kind", "join");
  await expect(page.getByTestId("invite-accept")).toHaveCount(0);
});

test("a register link gives the right to register a project, with harness init and register in its list", async ({ page, admin }) => {
  const path = pathOf(await admin.invite({ kind: "register", days: 1 }));
  await page.goto(path);
  await expect(page.getByTestId("invite-register")).toHaveText("The right to register one project of your own on this hub");
  await expect(page.getByTestId("invite-project")).toHaveCount(0);

  const founder = newAccount("founder");
  await signInFromInvite(page, founder, path);
  await expect(page.getByTestId("invite-viewer")).toContainText(founder.login);
  const list = await accept(page);
  await expect(page.getByTestId("invite-accepted")).toHaveAttribute("data-kind", "register");
  await expect(command(list, "invite-command-login")).toHaveText(`evo-agents hub login --url ${WEB_ORIGIN}`);
  await expect(command(list, "invite-command-harness")).toHaveText("evo-agents harness init");
  await expect(command(list, "invite-command-register")).toHaveText("evo-agents hub project register");
  await list.getByTestId("invite-project-name").fill("acme-tools");
  await expect(command(list, "invite-command-harness")).toHaveText("evo-agents harness init --project acme-tools");
  await list.getByTestId("invite-project-name").fill("Acme Tools; rm -rf ~");
  await expect(list.getByTestId("invite-project-name")).toHaveAttribute("aria-invalid", "true");
  await expect(command(list, "invite-command-harness")).toHaveText("evo-agents harness init"); // never a name it would refuse
  await expect(command(list, "invite-command-service")).toHaveText("evo-agents worker service install");
  await expect(list.getByTestId("invite-open-home")).toHaveAttribute("href", "/");
});

test("a link bound to one login shows its project and role to that login only", async ({ page, admin }) => {
  const project = uniqueName("bound");
  await admin.registerProject(project);
  const right = newAccount("right");
  const other = newAccount("other");
  const path = pathOf(await admin.invite({ kind: "join", project, role: "reader", max_level: "public", for_login: right.login, days: 3 }));

  await page.goto(path);
  await expect(page.getByTestId("invite-summary")).toHaveAttribute("data-state", "signed-out");
  await expect(page.getByTestId("invite-hidden")).toBeVisible();
  await expect(page.getByTestId("invite-project")).toHaveCount(0);
  await expect(page.getByTestId("invite-role")).toHaveCount(0);
  expect(await page.content()).not.toContain(project); // not in the HTML, nor in the data it hydrates from

  await signInFromInvite(page, other, path);
  await expect(page.getByTestId("invite-summary")).toHaveAttribute("data-state", "other-login");
  await expect(page.getByTestId("invite-not-for-you")).toContainText(`not ${other.login}`);
  await expect(page.getByTestId("invite-accept")).toHaveCount(0);
  expect(await page.content()).not.toContain(project);

  await page.getByTestId("invite-sign-out").click();
  await expect(page.getByTestId("invite-summary")).toHaveAttribute("data-state", "signed-out");
  await expect(page).toHaveURL(`${WEB_ORIGIN}${path}`);

  await page.getByTestId("invite-sign-in").waitFor();
  await signInFromInvite(page, right, path);
  await expect(page.getByTestId("invite-summary")).toHaveAttribute("data-state", "for-viewer");
  await expect(page.getByTestId("invite-viewer")).toContainText(right.login);
  await expect(page.getByTestId("invite-project")).toHaveText(project);
  await expect(page.getByTestId("invite-role")).toContainText("Reader");
  const list = await accept(page);
  await expect(list.getByTestId("invite-open-project")).toHaveAttribute("href", `/p/${project}`);
  await expect(list.getByTestId("invite-step-worker")).toHaveCount(0); // a reader runs no plans
  await expect(list.getByTestId("invite-step-run")).toHaveCount(0);
});

test("a hub admin makes a link that shows once, finds it in the list and revokes it", async ({ page, admin, signInAs }) => {
  const project = uniqueName("made");
  await admin.registerProject(project);
  const invitee = newAccount("invitee");
  await signInAs(ADMIN_ACCOUNT);
  await page.goto("/admin/invites");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Invite links");

  await page.getByTestId("invite-open").click();
  const dialog = page.getByTestId("invite-dialog");
  await dialog.getByTestId("invite-kind-join").click();
  await dialog.getByTestId("invite-project").selectOption(project);
  await dialog.getByTestId("invite-role-writer").click();
  await dialog.getByTestId("invite-days").fill("3");
  await dialog.getByTestId("invite-for-login").fill(invitee.login);
  await dialog.getByTestId("invite-create").click();

  const link = dialog.getByTestId("invite-created-link").locator("code");
  await expect(link).toHaveText(new RegExp(`^${WEB_ORIGIN}/invite/[^/]+$`));
  const path = new URL((await link.textContent()) ?? "").pathname;
  await expect(dialog.getByTestId("invite-created-link").getByRole("button", { name: "Copy the invite link" })).toBeVisible();
  await dialog.getByTestId("invite-created-done").click();
  await expect(dialog).toBeHidden();
  // Shown once: the dialog opens on a new form, and the list holds no token.
  await page.getByTestId("invite-open").click();
  await expect(page.getByTestId("invite-dialog").getByTestId("invite-created")).toHaveCount(0);
  await expect(page.getByTestId("invite-dialog").getByTestId("invite-create")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("invite-dialog")).toBeHidden();

  const table = page.locator("#main").getByTestId("invites-table");
  const row = table.locator("tbody tr").filter({ hasText: invitee.login });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText(project);
  await expect(row.getByTestId("invite-status")).toHaveAttribute("data-state", "open");
  await expect(table).not.toContainText(path.split("/").pop() ?? "");

  await row.getByRole("button", { name: /^Revoke invite link / }).click();
  const confirm = page.getByTestId("revoke-invite-dialog");
  await expect(confirm).toContainText("Nobody can accept it any more.");
  await confirm.getByTestId("revoke-invite-dialog-confirm").click();
  await expect(confirm).toBeHidden();
  await expect(toast(page, /Invite link \d+ revoked/)).toBeVisible();
  await expect(row.getByTestId("invite-status")).toHaveAttribute("data-state", "revoked");
  await expect(row.getByRole("button", { name: /^Revoke invite link / })).toHaveCount(0);

  await signOut(page);
  await page.goto(path);
  await expect(page.getByTestId("invite-closed")).toContainText("This invite link was revoked");
});

test("a member without a grant is told an admin may send an invite link", async ({ page, signInAs }) => {
  await signInAs(newAccount("nogrant"));
  await expect(page.locator("#main").getByTestId("state-empty")).toContainText("or sends you an invite link");
});
