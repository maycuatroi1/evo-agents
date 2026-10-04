import path from "node:path";

import { expect, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import { apiOf, memoryFile, memoryProject, putMemory } from "./support/memories";
import { packSkill, publishSkill } from "./support/skills";

/**
 * Review screenshots of the memories and skills pages, light and dark, at 1440 and 375 px, written to
 * E2E_SCREENSHOT_DIR. Skipped unless it is set; it asserts nothing beyond the pages being ready, so it never belongs
 * in a verify command.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

const RUNBOOK = `# Deploy runbook

Releases go out from **main** after the checks pass. Never deploy on Friday afternoons.

## Steps

1. Tag the release: \`git tag v1.4.0\`
2. Run the pipeline and watch the canary for 15 minutes.
3. Announce in the release channel.

- [x] Canary dashboards linked
- [ ] Rollback drill this quarter

| Environment | Owner | Window |
| --- | --- | --- |
| staging | platform | any time |
| production | platform | Mon to Thu, 09:00 to 16:00 |

\`\`\`sh
make release VERSION=1.4.0
\`\`\`

> If the canary error rate passes 1 %, roll back first and ask questions later.

See the [incident guide](https://example.org/incidents) for escalation.
`;

test.describe("memories and skills screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(120_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`memories and skills in ${scheme}`, async ({ page, admin, signInAs }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width: 1440, height: 900 });
      const account = newAccount("reviewer");
      const project = uniqueName("atlas");
      await admin.registerProject(project, memoryProject());
      await admin.grant(project, account.login, "writer", "customer");
      const api = await apiOf(account);
      const runbook = await putMemory(api, {
        project,
        name: "deploy-runbook.md",
        level: "customer",
        body: memoryFile("Deploy runbook", "How a release goes out, step by step", "Draft"),
      });
      await putMemory(api, {
        project,
        name: "deploy-runbook.md",
        ifRevision: 1,
        body: memoryFile("Deploy runbook", "How a release goes out, step by step", RUNBOOK.replace("15 minutes", "10 minutes")),
      });
      await putMemory(api, {
        project,
        name: "deploy-runbook.md",
        ifRevision: 2,
        body: memoryFile("Deploy runbook", "How a release goes out, step by step", RUNBOOK),
      });
      const seeds = [
        { location: "api", name: "api-conventions.md", type: "reference", level: "public", title: "API conventions", text: "Errors are `{error, message, request_id}`; every list pages by cursor." },
        { location: "web", name: "web-release.md", type: "project", level: "internal", title: "Web release notes", text: "The web ships with the API; the schema check fails CI when they drift." },
        { location: "harness", name: "my-preferences.md", type: "user", level: "internal", title: "My preferences", text: "Prefers Vietnamese reports with full diacritics." },
        { location: "api", name: "review-feedback.md", type: "feedback", level: "customer", title: "Review feedback", text: "Keep pull requests under 400 lines." },
        { location: "harness", name: "kg-labels.md", type: "project", level: "internal", title: "Knowledge graph labels", text: "Labels flow from knowledge.yaml; the hub sink decides what leaves a machine." },
      ] as const;
      for (const seed of seeds) {
        await putMemory(api, {
          project,
          location: seed.location,
          name: seed.name,
          type: seed.type,
          level: seed.level,
          body: memoryFile(seed.title, seed.text.split(";")[0], seed.text, seed.type),
        });
      }
      await putMemory(api, { name: "reading-list.md", body: memoryFile("Reading list", "Papers to read this month", "- Harness engineering\n- Effective agents") });

      const first = await packSkill("release-checklist", "Run the release checklist.", "Use when cutting a release of the platform");
      await publishSkill(api, first, "release-checklist", project, { repo: "api", commit: "0123abc" });
      const second = await packSkill("release-checklist", "Run the release checklist, then announce.", "Use when cutting a release of the platform");
      await publishSkill(api, second, "release-checklist", project, { repo: "example-org/agent-skills", commit: "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432" });
      const adminApi = await apiOf(ADMIN_ACCOUNT);
      const house = uniqueName("house-style");
      await publishSkill(adminApi, await packSkill(house, "Write like the team."), house, null, { repo: "example-org/agent-skills", commit: "abcdef1" });

      await signInAs(account);
      const shot = async (name: string, fullPage = false) =>
        page.screenshot({ path: path.join(dir!, `${name}-${scheme}.png`), fullPage });

      await page.goto(`/p/${project}/memories`);
      await expect(page.locator("#main").getByTestId("memories-table")).toBeVisible();
      await shot("memories-list");
      await page.goto(`/p/${project}/memories/${runbook.id}`);
      await expect(page.locator("#main").getByTestId("memory-markdown")).toBeVisible();
      await shot("memory-detail", true);
      await page.goto(`/p/${project}/memories/${runbook.id}?revision=1`);
      await expect(page.locator("#main").getByTestId("old-revision")).toBeVisible();
      await shot("memory-revision");
      await page.goto("/memories");
      await expect(page.locator("#main").getByTestId("memories-table")).toBeVisible();
      await shot("memories-personal");
      await page.goto(`/p/${project}/skills`);
      await expect(page.locator("#main").getByTestId("skills-table")).toBeVisible();
      await shot("skills-project");
      await page.goto(`/p/${project}/skills/release-checklist`);
      await expect(page.locator("#main").getByTestId("skill-versions")).toBeVisible();
      await shot("skill-detail", true);
      await page.goto("/skills");
      await expect(page.locator("#main").getByTestId("skills-table")).toBeVisible();
      await shot("skills-global");

      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto(`/p/${project}/memories`);
      await expect(page.locator("#main").getByTestId("memories-table")).toBeVisible();
      await shot("memories-list-375", true);
      await page.goto(`/p/${project}/memories/${runbook.id}`);
      await expect(page.locator("#main").getByTestId("memory-markdown")).toBeVisible();
      await shot("memory-detail-375", true);
      await page.goto(`/p/${project}/skills/release-checklist`);
      await expect(page.locator("#main").getByTestId("skill-versions")).toBeVisible();
      await shot("skill-detail-375", true);
      await page.goto(`/p/${project}/skills`);
      await expect(page.locator("#main").getByTestId("skills-table")).toBeVisible();
      await shot("skills-project-375", true);
    });
  }
});
