import { test as base } from "@playwright/test";

import { signIn } from "./auth";
import { DEPLOYED_BASE_URL } from "./env";
import { type Account, HubAdmin, newAccount, type Role, uniqueName } from "./hub";

type Grant = { role: Role; maxLevel: string };

export type Member = Account & { projects: string[] };

type Fixtures = {
  /** Sign the page in as `account` through the fake GitHub. */
  signInAs: (account: Account) => Promise<void>;
  /** A fresh member with grants on new projects, signed in on `page` (local stack only). */
  member: (grants?: Grant[]) => Promise<Member>;
};

type WorkerFixtures = { admin: HubAdmin };

export const test = base.extend<Fixtures, WorkerFixtures>({
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
