import { STACK_URL } from "./env";
import type { Account } from "./hub";
import { enablePlugin } from "./plugins";

/**
 * The core plugins crm and marketing (plan autonomous-oss-growth, step 20): enabled through the real API, then their
 * rows written as their jobs would leave them (hub_stack.py, POST /growth/seed, growth_seed.py), since the stack finds
 * no contact on GitHub and posts on no channel.
 */
export function contactsPath(project: string): string {
  return `/p/${project}/x/crm/contacts`;
}

export function contactPath(project: string, login: string): string {
  return `${contactsPath(project)}/${login}`;
}

export function marketingPath(project: string, page: "overview" | "posts" | "weekly" = "overview"): string {
  return `/p/${project}/x/marketing/${page}`;
}

/** The logins growth_seed.py gives the four contacts, lead first; the lead's is 39 characters with `long`. */
export const CONTACTS = { lead: "ana-lead", longLead: "a-very-long-github-login-that-never-wra", invited: "binh-invited", activated: "chi-active", retained: "dung-kept" };

export type Seeded = { contacts: string[]; channels: Record<string, number>; posts: Record<string, number> };

/** Enable crm and marketing in `project` as `account`, an admin of it; neither asks for a grant. */
export async function enableGrowth(account: Account, project: string): Promise<void> {
  await enablePlugin(account, project, "crm", []);
  await enablePlugin(account, project, "marketing", []);
}

/**
 * crm and marketing enabled in `project` by `owner`, an admin of it, then four contacts (one per stage), four channel
 * accounts (github, x with its breaker open, linkedin with its session expired, `owner`'s facebook_group), a post of
 * each outcome of the gate, and the month's budget with `spent` dollars spent of the 50 the cap allows.
 */
export async function seedGrowth(owner: Account, project: string, options: { spent?: number; long?: boolean } = {}): Promise<Seeded> {
  await enableGrowth(owner, project);
  const response = await fetch(`${STACK_URL}/growth/seed`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ project, owner: owner.login, ...options }),
  });
  if (!response.ok) throw new Error(`hub_stack /growth/seed: ${response.status} ${await response.text()}`);
  return (await response.json()) as Seeded;
}
