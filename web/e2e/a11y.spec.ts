import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, uniqueName } from "./support/hub";
import { apiOf, memoryFile, putMemory } from "./support/memories";
import { packSkill, publishSkill } from "./support/skills";

/**
 * axe (WCAG 2.2 A and AA rules) on every page of the web, in light and dark: no serious or critical violation.
 * Later steps add their pages to PAGES; each entry opens the page in a given state and waits for its content.
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

    test(`hub admin page has no serious axe violation (${scheme})`, async ({ page, signInAs }) => {
      test.skip(isDeployed, "signs in as the stack's hub admin");
      await signInAs(ADMIN_ACCOUNT);
      await page.goto("/admin");
      await expect(page.getByTestId("admin-stats")).toBeVisible();
      await expectNoSeriousViolations(page, `/admin as hub admin (${scheme})`);
    });
  });
}
