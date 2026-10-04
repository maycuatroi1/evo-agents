import { describe, expect, it } from "vitest";

import en from "./en.json";
import vi from "./vi.json";

type Tree = { [key: string]: string | Tree };

function leaves(tree: Tree, prefix = ""): Map<string, string> {
  const found = new Map<string, string>();
  for (const [key, value] of Object.entries(tree)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") found.set(path, value);
    else for (const [inner, text] of leaves(value, path)) found.set(inner, text);
  }
  return found;
}

const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\s*[,}]/g)].map((match) => match[1]).sort();

describe("messages", () => {
  const viLeaves = leaves(vi);
  const enLeaves = leaves(en);

  it("has the same keys in Vietnamese and English", () => {
    expect([...enLeaves.keys()].sort()).toEqual([...viLeaves.keys()].sort());
  });

  it("uses the same placeholders and tags in both languages", () => {
    for (const [key, text] of viLeaves) {
      expect(placeholders(enLeaves.get(key) ?? ""), key).toEqual(placeholders(text));
      expect((enLeaves.get(key) ?? "").match(/<\/?\w+>/g), key).toEqual(text.match(/<\/?\w+>/g));
    }
  });

  it("has no empty text and no middle dot used as a separator", () => {
    for (const [key, text] of [...viLeaves, ...enLeaves]) {
      expect(text.trim(), key).not.toBe("");
      expect(text, key).not.toContain("·");
    }
  });

  it("is written in Vietnamese with diacritics", () => {
    expect(vi.login.github).toBe("Đăng nhập bằng GitHub");
    expect(vi.states.forbiddenTitle).toBe("Bạn không có quyền xem trang này");
  });
});
