/**
 * The GitHub page of the commit a skill version was published from. The hub records `source_repo` as the
 * publisher typed it: `owner/repo`, a GitHub URL, or only a repo's name (`agent-skills`), which names a repo of the
 * project when its origin is on GitHub. Anything else gets no link: a wrong link is worse than none.
 */

export type RepoOrigin = { name: string; origin?: string | null };

const OWNER = "[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})";
const REPO = "[A-Za-z0-9._-]{1,100}";
const SLUG = new RegExp(`^(${OWNER})/(${REPO})$`);
const ORIGINS = [
  new RegExp(`^(?:https?://)?(?:www\\.)?github\\.com/(${OWNER})/(${REPO}?)(?:\\.git)?/?$`, "i"),
  new RegExp(`^(?:ssh://)?git@github\\.com[:/](${OWNER})/(${REPO}?)(?:\\.git)?/?$`, "i"),
];
const COMMIT = /^[0-9a-f]{7,64}$/;

export type GitHubRepo = { owner: string; repo: string };

/** owner/repo of a GitHub origin or slug; null for anything else, another host included. */
export function githubRepo(text: string | null | undefined): GitHubRepo | null {
  const value = text?.trim();
  if (!value) return null;
  for (const pattern of [SLUG, ...ORIGINS]) {
    const match = pattern.exec(value);
    if (match) {
      const repo = match[2].replace(/\.git$/i, "");
      if (repo && repo !== "." && repo !== "..") return { owner: match[1], repo };
    }
  }
  return null;
}

export function githubCommitUrl(
  sourceRepo: string | null | undefined,
  commit: string | null | undefined,
  repos: readonly RepoOrigin[] = [],
): string | null {
  if (!sourceRepo || !commit || !COMMIT.test(commit)) return null;
  const named = repos.find((repo) => repo.name === sourceRepo);
  const found = githubRepo(sourceRepo) ?? githubRepo(named?.origin);
  if (!found) return null;
  return `https://github.com/${encodeURIComponent(found.owner)}/${encodeURIComponent(found.repo)}/commit/${commit}`;
}
