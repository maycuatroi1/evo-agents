import { describe, expect, it } from "vitest";

import en from "../../../messages/en.json";
import vi from "../../../messages/vi.json";
import { CLAIMS } from "./claims";
import { NEEDS } from "./needs";

describe("the benefits on the product page", () => {
  it("cover the rows in both languages, each once", () => {
    const keys = NEEDS.map((need) => need.key);
    expect(new Set(keys).size).toBe(keys.length);
    for (const messages of [en, vi]) expect([...keys].sort()).toEqual(Object.keys(messages.product.needs.rows).sort());
  });

  it("use claims backed by public documentation", () => {
    for (const need of NEEDS) expect(Object.keys(CLAIMS), need.key).toContain(need.claim);
  });
});
