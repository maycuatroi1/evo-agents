import { test as base } from "@playwright/test";

import type { Locale } from "../../src/i18n/locales";

import { signIn } from "./auth";
import { DEPLOYED_BASE_URL } from "./env";
import { type Account, HubAdmin, newAccount, type Role, uniqueName } from "./hub";
import { setUiLocale } from "./locale";

type Grant = { role: Role; maxLevel: string };

export type Member = Account & { projects: string[] };

type Options = {
  /**
   * The UI language the spec reads. `null` (the default) sends no locale cookie, so pages render in the web's
   * default, English; a spec that matches Vietnamese copy says `test.use({ uiLocale: "vi" })`.
   */
  uiLocale: Locale | null;
};

type Fixtures = {
  /** Sign the page in as `account` through the fake GitHub. */
  signInAs: (account: Account) => Promise<void>;
  /** A fresh member with grants on new projects, signed in on `page` (local stack only). */
  member: (grants?: Grant[]) => Promise<Member>;
};

type WorkerFixtures = { admin: HubAdmin };

export const test = base.extend<Options & Fixtures, WorkerFixtures>({
  uiLocale: [null, { option: true }],
  context: async ({ context, uiLocale }, use) => {
    if (uiLocale) await setUiLocale(context, uiLocale);
    await use(context);
  },
  admin: [
    async ({}, use) => {
      await use(new HubAdmin());
    },
    { scope: "worker" },
  ],
  signInAs: async ({ page }, use) => {
    await use((account) => signIn(page, account));
  },
  member: async ({ page, admin }, use) => {
    await use(async (grants = [{ role: "reader", maxLevel: "internal" }]) => {
      const account = newAccount("member");
      const projects: string[] = [];
      for (const grant of grants) {
        const project = uniqueName("proj");
        await admin.registerProject(project);
        await admin.grant(project, account.login, grant.role, grant.maxLevel);
        projects.push(project);
      }
      await signIn(page, account);
      return { ...account, projects };
    });
  },
});

export { expect } from "@playwright/test";

/** In a deployed run the page is signed in by EVO_E2E_STORAGE_STATE; locally, as a fresh member. */
export const isDeployed = DEPLOYED_BASE_URL !== null;
