import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import en from "../../../messages/en.json";
import vi from "../../../messages/vi.json";
import { CLAIMS, type ClaimKey } from "./claims";
import { docHref, REPO_URL } from "./links";

// Each sentence the product page says about evo-agents must link to the public docs section that describes it: a
// claim whose sentence holds no link, whose tag has no section, or whose section is gone fails here.
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
const sentences = (text: string) => text.split(/(?<=[.!?]["”]?)\s+/).filter((part) => part.trim());

describe("the product page's claims", () => {
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

  it("put a link in every sentence", () => {
    for (const [language, claims] of Object.entries(LANGUAGES)) {
      for (const [key, text] of Object.entries(claims)) {
        for (const sentence of sentences(text)) expect(sentence, `${language} ${key}`).toMatch(/<\w+>/);
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

  it("find a heading the way GitHub names it", () => {
    expect(slug("What `kg_impact` follows")).toBe("what-kg_impact-follows");
    expect(slug("Decisions and notices")).toBe("decisions-and-notices");
    expect(slug("A plan run with a session for each step")).toBe("a-plan-run-with-a-session-for-each-step");
  });

  it("make none of the claims the plan rules out", () => {
    const copy = JSON.stringify(en.product).toLowerCase();
    for (const banned of [
      /account (sharing|rotation|pool)/,
      /curator/,
      /cheaper|save (money|cost)|cost saving/,
      /testimonial|trusted by|loved by/,
      /\bmemor(y|ies)\b|\bskills? sync|\bsync(s|ing)? (your )?(memor|skill)/,
    ]) {
      expect(copy).not.toMatch(banned);
    }
  });
});
