import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import { ACTIVE_PLAN, EVIDENCE_STEP, open, seedPlans } from "./support/plans";

/**
 * axe (WCAG 2.2 A and AA rules) on every page of the web, in light and dark: no serious or critical violation.
 * Later steps add their pages to PAGES; each entry opens the page in a given state and waits for its content.
 */
type Context = { page: Page; me: Member; hidden: string };
type Entry = { name: string; open: (context: Context) => Promise<void>; deployed?: boolean };

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
    name: "small screen with the sidebar open",
    open: async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/");
      await page.getByRole("button", { name: "Ẩn hoặc hiện thanh bên" }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
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
  });
}
