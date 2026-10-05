import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, machineToken, newAccount, uniqueName } from "./support/hub";
import { graphReady, grantOn, HUB_NODE, kgPath, nodePath, sharedKg } from "./support/kg";
import { apiOf, memoryFile, putMemory } from "./support/memories";
import { ACTIVE_PLAN, EVIDENCE_STEP, open, seedPlans } from "./support/plans";
import { packSkill, publishSkill } from "./support/skills";

/**
 * axe (WCAG 2.2 A and AA rules) on every page of the web, in light and dark: no serious or critical violation.
 * Later steps add their pages to PAGES; each entry opens the page in a given state and waits for its content.
 * Pages render in the default language, English, and entries find what they click by role, test id or ARIA
 * state rather than by copy.
 */
type Context = { page: Page; me: Member; hidden: string };
type Entry = { name: string; open: (context: Context) => Promise<void>; deployed?: boolean };

/** Memories in the first project of `me` (a writer up to customer; the project's hub sink clears internal). */
async function seedMemories(me: Member) {
  const api = await apiOf(me);
  const project = me.projects[0];
  const text = "## Steps\n\n1. Tag\n2. Ship\n\n- [x] checked\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n```\nmake\n```";
  const runbook = await putMemory(api, { project, name: "runbook.md", body: memoryFile("Runbook", "Ship", "Draft") });
  await putMemory(api, { project, name: "runbook.md", ifRevision: 1, body: memoryFile("Runbook", "Ship", text) });
  await putMemory(api, {
    project,
    location: "api",
    name: "conventions.md",
    type: "reference",
    level: "public",
    body: memoryFile("Conventions", "Paging", "By cursor.", "reference"),
  });
  await putMemory(api, { project, name: "mine.md", type: "user", body: memoryFile("Mine", "Private", "x", "user") });
  return { runbook };
}

async function seedSkill(me: Member) {
  const bundle = await packSkill("team-notes", "Notes.", "Use when writing notes");
  await publishSkill(await apiOf(me), bundle, "team-notes", me.projects[0], { repo: "example-org/skills", commit: "0123abc" });
}

const PAGES: Entry[] = [
  {
    name: "my projects",
    deployed: true,
    open: async ({ page }) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    },
  },
  {
    name: "project picker open",
    deployed: true,
    open: async ({ page }) => {
      await page.goto("/");
      await page.getByTestId("project-switcher").click();
      await expect(page.getByTestId("project-switcher-menu")).toBeVisible();
    },
  },
  {
    name: "user menu open",
    deployed: true,
    open: async ({ page }) => {
      await page.goto("/");
      await page.getByTestId("user-menu").click();
      await expect(page.getByTestId("user-menu-content")).toBeVisible();
    },
  },
  {
    name: "project overview",
    open: async ({ page, me }) => {
      await page.goto(`/p/${me.projects[0]}`);
      await expect(page.getByTestId("label-ladder")).toBeVisible();
    },
  },
  {
    name: "no access (403)",
    open: async ({ page }) => {
      await page.goto("/admin");
      await expect(page.getByTestId("state-forbidden")).toBeVisible();
    },
  },
  {
    name: "project not found",
    open: async ({ page, hidden }) => {
      await page.goto(`/p/${hidden}`);
      await expect(page.getByTestId("state-not-found")).toBeVisible();
    },
  },
  {
    name: "plans list",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans`);
      await expect(page.getByTestId("plans-table-active")).toBeVisible();
    },
  },
  {
    name: "plan board with its sections open",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}`);
      await expect(page.getByTestId("step-board")).toBeVisible();
      await page.getByTestId("board-column-done").getByRole("button").click();
      for (const summary of await page.locator("details > summary").all()) await summary.click();
    },
  },
  {
    name: "plan steps as a list",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}`);
      await page.getByTestId("steps-view-list").click();
      await expect(page.getByTestId("steps-table")).toBeVisible();
    },
  },
  {
    name: "plan step with evidence",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}/steps/${EVIDENCE_STEP}`);
      await expect(page.getByTestId("step-evidence")).toBeVisible();
    },
  },
  {
    name: "plan revisions and diff",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}/revisions?from=1&to=2`);
      await expect(page.getByTestId("diff-line").first()).toBeVisible();
    },
  },
  {
    name: "plan diff side by side",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}/revisions?from=1&to=3`);
      await page.getByTestId("diff-mode-split").click();
      await expect(page.getByTestId("diff-mode-split")).toHaveAttribute("aria-pressed", "true");
    },
  },
  {
    name: "plan not found",
    open: async ({ page, me }) => {
      await open(page, `/p/${me.projects[0]}/plans/no-such-plan`);
      await expect(page.getByTestId("state-not-found")).toBeVisible();
    },
  },
  {
    name: "knowledge graph status and search",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "customer");
      await open(page, `${kgPath(kg.project)}?q=runbook`);
      await expect(page.getByTestId("kg-results")).toBeVisible();
      await expect(page.getByTestId("kg-latest-build")).toBeVisible();
    },
  },
  {
    name: "knowledge graph node with its neighbourhood",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "customer");
      await open(page, nodePath(kg.project, HUB_NODE));
      await graphReady(page);
      await expect(page.getByTestId("kg-truncated")).toBeVisible();
    },
  },
  {
    name: "knowledge graph node on a small screen",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "customer");
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, nodePath(kg.project, "requirement:KB-01", 1));
      await expect(page.getByTestId("kg-neighbours")).toBeVisible();
    },
  },
  {
    name: "knowledge graph node not found",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "internal");
      await open(page, nodePath(kg.project, "deals:doc:acme-contract"));
      await expect(page.getByTestId("state-not-found")).toBeVisible();
    },
  },
  {
    name: "project memories",
    open: async ({ page, me }) => {
      await seedMemories(me);
      await page.goto(`/p/${me.projects[0]}/memories`);
      await expect(page.locator("#main").getByTestId("memories-table")).toBeVisible();
    },
  },
  {
    name: "memory detail with its history",
    open: async ({ page, me }) => {
      const { runbook } = await seedMemories(me);
      await page.goto(`/p/${me.projects[0]}/memories/${runbook.id}`);
      await expect(page.locator("#main").getByTestId("memory-markdown")).toBeVisible();
      await expect(page.locator("#main").getByTestId("revision-history")).toBeVisible();
    },
  },
  {
    name: "memory search without results",
    open: async ({ page, me }) => {
      await page.goto(`/p/${me.projects[0]}/memories?q=nothing-matches-this`);
      await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "personal memories",
    open: async ({ page, me }) => {
      await putMemory(await apiOf(me), { name: "reading-list.md", body: memoryFile("Reading list", "Papers", "- one") });
      await page.goto("/memories");
      await expect(page.locator("#main").getByTestId("memories-table")).toBeVisible();
    },
  },
  {
    name: "project skills",
    open: async ({ page, me }) => {
      await seedSkill(me);
      await page.goto(`/p/${me.projects[0]}/skills`);
      await expect(page.locator("#main").getByTestId("skills-table")).toBeVisible();
    },
  },
  {
    name: "skill detail",
    open: async ({ page, me }) => {
      await seedSkill(me);
      await page.goto(`/p/${me.projects[0]}/skills/team-notes`);
      await expect(page.locator("#main").getByTestId("skill-versions")).toBeVisible();
    },
  },
  {
    name: "shared skills",
    open: async ({ page }) => {
      await page.goto("/skills");
      await expect(page.locator("#main").getByRole("heading", { level: 1 })).toBeVisible();
    },
  },
  {
    name: "small screen with the sidebar open",
    open: async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/");
      await page.locator('button[data-sidebar="trigger"]').click();
      await expect(page.getByRole("dialog")).toBeVisible();
    },
  },
];

/** The admin area, opened by the stack's hub admin; each entry waits for its content (step 25). */
const ADMIN_PAGES: { name: string; open: (page: Page) => Promise<void> }[] = [
  {
    name: "admin members",
    open: async (page) => {
      await page.goto("/admin/members");
      await expect(page.getByTestId("members-table")).toBeVisible();
    },
  },
  {
    name: "admin grant dialog, form and confirmation",
    open: async (page) => {
      await page.goto("/admin/members");
      await page.getByTestId("grant-open").click();
      const dialog = page.getByTestId("grant-dialog");
      await dialog.getByTestId("grant-continue").click(); // shows the field errors
      await expect(dialog.getByTestId("grant-login")).toHaveAttribute("aria-invalid", "true");
      await expectNoSeriousViolations(page, "grant form with errors");
      await dialog.getByTestId("grant-login").fill("e2e-someone");
      await dialog.getByTestId("grant-project").selectOption({ index: 1 });
      await dialog.getByTestId("grant-continue").click();
      await expect(page.getByTestId("grant-summary")).toBeVisible();
    },
  },
  {
    name: "admin member page",
    open: async (page) => {
      await page.goto(`/admin/members/${ADMIN_ACCOUNT.login}`);
      await expect(page.getByTestId("member-tokens")).toBeVisible();
      await expect(page.getByTestId("member-activity")).toBeVisible();
    },
  },
  {
    name: "admin tokens with the revoke confirmation open",
    open: async (page) => {
      await page.goto("/admin/tokens");
      await expect(page.getByTestId("tokens-table")).toBeVisible();
      await expectNoSeriousViolations(page, "tokens");
      await page.getByTestId("tokens-table").locator('[data-testid^="revoke-token-"]').first().click();
      await expect(page.getByTestId("revoke-token-dialog")).toBeVisible();
    },
  },
  {
    name: "admin audit, filtered and on its second page",
    open: async (page) => {
      const busy = newAccount("busy");
      for (let i = 0; i < 26; i += 1) await machineToken(busy); // more rows than one page holds
      await page.goto(`/admin/audit?actor=${busy.login}&limit=25`);
      await expect(page.getByTestId("audit-table")).toBeVisible();
      await expectNoSeriousViolations(page, "audit");
      await page.getByTestId("pager-next").click();
      await expect(page.getByTestId("pager-page")).toHaveText(/\b2$/); // "Page 2" in English, "Trang 2" in Vietnamese
    },
  },
  {
    name: "admin audit with no matching row",
    open: async (page) => {
      await page.goto(`/admin/audit?actor=${uniqueName("nobody")}`);
      await expect(page.getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "admin members and audit on a small screen",
    open: async (page) => {
      // Scoped to <main>: a viewport change while a page streams in can leave the server's copy hidden outside it.
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/admin/audit");
      await expect(page.locator("#main").getByTestId("audit-table")).toBeVisible();
      await expectNoSeriousViolations(page, "audit at 375 px");
      await page.goto("/admin/members");
      await expect(page.locator("#main").getByTestId("members-table")).toBeVisible();
    },
  },
];

for (const scheme of ["light", "dark"] as const) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme, reducedMotion: "reduce" });

    test.describe("signed out", () => {
      test.use({ storageState: { cookies: [], origins: [] } });

      test(`sign-in page has no serious axe violation (${scheme}) @deployed`, async ({ page }) => {
        await page.goto("/login");
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
        await expectNoSeriousViolations(page, `/login (${scheme})`);
      });
    });

    for (const entry of PAGES) {
      const tag = entry.deployed ? " @deployed" : "";
      test(`${entry.name} has no serious axe violation (${scheme})${tag}`, async ({ page, member, admin }) => {
        test.skip(isDeployed && !entry.deployed, "seeds data through the local stack");
        let me: Member = { login: "", id: 0, projects: [] };
        let hidden = "";
        if (!isDeployed) {
          me = await member([
            { role: "writer", maxLevel: "customer" },
            { role: "reader", maxLevel: "public" },
          ]);
          hidden = uniqueName("hidden");
          await admin.registerProject(hidden);
        }
        await entry.open({ page, me, hidden });
        await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
        await expectNoSeriousViolations(page, `${entry.name} (${scheme})`);
      });
    }

    test(`plans without a grant (403) have no serious axe violation (${scheme})`, async ({ page, admin, signInAs }) => {
      test.skip(isDeployed, "signs in as the stack's hub admin");
      const project = uniqueName("plans");
      const writer = newAccount("writer");
      await admin.registerProject(project);
      await admin.grant(project, writer.login, "writer", "internal");
      await seedPlans(writer, project);
      await signInAs(ADMIN_ACCOUNT);
      await open(page, `/p/${project}/plans/${ACTIVE_PLAN}`);
      await expect(page.getByTestId("state-forbidden")).toBeVisible();
      await expectNoSeriousViolations(page, `plans without a grant (${scheme})`);
    });

    test(`hub admin page has no serious axe violation (${scheme})`, async ({ page, signInAs }) => {
      test.skip(isDeployed, "signs in as the stack's hub admin");
      await signInAs(ADMIN_ACCOUNT);
      await page.goto("/admin");
      await expect(page.getByTestId("admin-stats")).toBeVisible();
      await expectNoSeriousViolations(page, `/admin as hub admin (${scheme})`);
    });

    for (const entry of ADMIN_PAGES) {
      test(`${entry.name} has no serious axe violation (${scheme})`, async ({ page, signInAs }) => {
        test.skip(isDeployed, "signs in as the stack's hub admin");
        await signInAs(ADMIN_ACCOUNT);
        await entry.open(page);
        await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
        await expectNoSeriousViolations(page, `${entry.name} (${scheme})`);
      });
    }
  });
}
