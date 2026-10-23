import { describe, expect, it } from "vitest";

import en from "../../../messages/en.json";
import { CLAIMS } from "./claims";
import { NEEDS, type Source } from "./needs";

// Each need the page answers rests on public sources with the day their counts were read: a need without one fails.
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const validDay = (day: string) => ISO_DAY.test(day) && !Number.isNaN(Date.parse(`${day}T00:00:00Z`));

function publicUrl(source: Source): boolean {
  const url = new URL(source.url);
  if (url.protocol !== "https:") return false;
  if (source.kind === "issue") return url.href === `https://github.com/${source.repo}/issues/${url.pathname.split("/").at(-1)}`;
  if (source.kind === "hn") return url.host === "news.ycombinator.com";
  return true;
}

describe("the needs of the product page", () => {
  it("are the rows of the message files, each once", () => {
    const keys = NEEDS.map((need) => need.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect([...keys].sort()).toEqual(Object.keys(en.product.needs.rows).sort());
  });

  it("each have a public source and the day its counts were read", () => {
    for (const need of NEEDS) {
      expect(need.sources.length, need.key).toBeGreaterThan(0);
      expect(validDay(need.checkedOn), need.key).toBe(true);
      for (const source of need.sources) {
        expect(publicUrl(source), source.url).toBe(true);
        expect(source.title.trim(), source.url).not.toBe("");
        if (source.kind === "issue") {
          expect(source.url, source.url).toMatch(/\/issues\/\d+$/);
          expect(Number.isInteger(source.reactions) && source.reactions > 0, source.url).toBe(true);
          if (source.closedOn) {
            expect(validDay(source.closedOn), source.url).toBe(true);
            expect(source.closedOn <= need.checkedOn, source.url).toBe(true);
          }
        }
        if (source.kind === "hn") expect(source.points > 0 && source.comments >= 0, source.url).toBe(true);
      }
      const urls = need.sources.map((source) => source.url);
      expect(new Set(urls).size, need.key).toBe(urls.length);
    }
  });

  it("each answer with claims the page links to the docs", () => {
    for (const need of NEEDS) {
      expect(need.claims.length, need.key).toBeGreaterThan(0);
      for (const claim of need.claims) expect(Object.keys(CLAIMS), need.key).toContain(claim);
    }
  });
});
