/**
 * Where the product page points: the public repository on GitHub, its docs on the default branch, and the issue form
 * that asks for an invite (.github/ISSUE_TEMPLATE/invite.yml).
 */
export const REPO_URL = "https://github.com/maycuatroi1/evo-agents";

/** A file of the repository, at an anchor of its headings: GitHub's slug of the heading. */
export type DocLink = { file: string; anchor?: string };

export function docHref({ file, anchor }: DocLink): string {
  return `${REPO_URL}/blob/main/${file}${anchor ? `#${anchor}` : ""}`;
}

export const README: DocLink = { file: "README.md", anchor: "evo-agents" };
export const ROADMAP: DocLink = { file: "docs/roadmap.md" };
export const LICENSE: DocLink = { file: "LICENSE" };

/** The issue form that asks for an invite: a GitHub login and what the person wants to do, no email. */
export const INVITE_FORM_URL = `${REPO_URL}/issues/new?template=invite.yml`;
