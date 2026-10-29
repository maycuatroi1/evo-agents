import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import en from "../../../messages/en.json";
import vi from "../../../messages/vi.json";
import { CLAIMS, type ClaimKey } from "./claims";
import { docHref, GETTING_STARTED, LOCAL_GRAPH, REPO_URL } from "./links";

// Each product claim has a public reference. Links and anchors must survive documentation moves.
const repoDir = fileURLToPath(new URL("../../../..", import.meta.url));
const LANGUAGES = { en: en.product.claims, vi: vi.product.claims } as const;

/** GitHub's anchor of a Markdown heading: lowercase, punctuation but `-` and `_` dropped, spaces to dashes. */
function slug(heading: string): string {
  return heading
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{M}\p{N}\p{Pc}\- ]/gu, "")
    .replace(/ /g, "-");
}

/** The anchors of a Markdown file's headings outside code fences, a repeated one with GitHub's -1, -2 suffix. */
function anchors(file: string): Set<string> {
  const found = new Set<string>();
  const seen = new Map<string, number>();
  let fence = false;
  for (const line of readFileSync(path.join(repoDir, file), "utf8").split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) fence = !fence;
    const heading = fence ? null : /^#{1,6}\s+(.+?)\s*#*\s*$/.exec(line);
    if (!heading) continue;
    const base = slug(heading[1]);
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    found.add(count ? `${base}-${count}` : base);
  }
  return found;
}

const tagsOf = (text: string) => [...new Set([...text.matchAll(/<(\w+)>/g)].map((match) => match[1]))];

describe("the product page's claims", () => {
  it("keeps the headline and example consistent with README", () => {
    const readme = readFileSync(path.join(repoDir, "README.md"), "utf8");
    expect(readme).toContain(en.product.hero.title);
    expect(readme).toContain(en.product.workflow.example);
  });

  it("are the claims of both message files", () => {
    for (const claims of Object.values(LANGUAGES)) expect(Object.keys(claims).sort()).toEqual(Object.keys(CLAIMS).sort());
  });

  it("link every tag of a claim, in both languages, and nothing else", () => {
    for (const [language, claims] of Object.entries(LANGUAGES)) {
      for (const [key, text] of Object.entries(claims)) {
        expect(tagsOf(text).sort(), `${language} ${key}`).toEqual(Object.keys(CLAIMS[key as ClaimKey]).sort());
      }
    }
  });

  it("give each claim a public reference without requiring a link in every sentence", () => {
    for (const [language, claims] of Object.entries(LANGUAGES)) {
      for (const [key, text] of Object.entries(claims)) {
        expect(text, `${language} ${key}`).toMatch(/<\w+>/);
      }
    }
  });

  it("link sections the public docs have", () => {
    for (const [key, links] of Object.entries(CLAIMS)) {
      for (const [tag, doc] of Object.entries(links)) {
        const where = `${key} <${tag}> ${doc.file}`;
        expect(existsSync(path.join(repoDir, doc.file)), where).toBe(true);
        expect(docHref(doc).startsWith(`${REPO_URL}/blob/main/`), where).toBe(true);
        if (doc.anchor && doc.file.endsWith(".md")) expect([...anchors(doc.file)], where).toContain(doc.anchor);
      }
    }
  });

  it("keep both onboarding routes pointed at the new guide", () => {
    for (const doc of [GETTING_STARTED, LOCAL_GRAPH]) {
      expect([...anchors(doc.file)]).toContain(doc.anchor);
    }
  });

  it("find a heading the way GitHub names it", () => {
    expect(slug("What `kg_impact` follows")).toBe("what-kg_impact-follows");
    expect(slug("Decisions and notices")).toBe("decisions-and-notices");
    expect(slug("A plan run with a session for each step")).toBe("a-plan-run-with-a-session-for-each-step");
  });

  it("avoid unsubstantiated promotional claims", () => {
    const copy = JSON.stringify(en.product).toLowerCase();
    for (const banned of [
      /account (sharing|rotation|pool)/,
      /curator/,
      /cheaper|save (money|cost)|cost saving/,
      /testimonial|trusted by|loved by/,
    ]) {
      expect(copy).not.toMatch(banned);
    }
  });
});
