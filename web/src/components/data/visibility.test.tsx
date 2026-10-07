// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import en from "../../../messages/en.json";
import { renderVi } from "@/test/render";

import { isKnownVisibility, VISIBILITY_LEVELS, VisibilityLevel, VisibilityTag } from "./visibility";

describe("VisibilityLevel", () => {
  it("shows the four levels of the default ladder as words, with the code in the tooltip", () => {
    renderVi(
      <ul>
        {VISIBILITY_LEVELS.map((level) => (
          <li key={level}>
            <VisibilityLevel level={level} />
          </li>
        ))}
      </ul>,
    );
    for (const [code, word] of [
      ["public", "Public"],
      ["internal", "Internal"],
      ["customer", "Customer"],
      ["secret", "Secret"],
    ]) {
      const shown = screen.getByText(word);
      expect(shown).toHaveAttribute("title", code);
      expect(shown).toHaveAttribute("data-level", code);
    }
  });

  it("keeps a level a project named itself as written, without a tooltip, and a missing one as a dash", () => {
    renderVi(
      <>
        <VisibilityLevel level="closed" testId="own" />
        <VisibilityLevel level={null} testId="none" />
      </>,
    );
    expect(screen.getByTestId("own")).toHaveTextContent("closed");
    expect(screen.getByTestId("own")).not.toHaveAttribute("title");
    expect(screen.getByTestId("none")).toHaveTextContent("-");
  });

  it("knows only the default ladder's codes", () => {
    expect(isKnownVisibility("internal")).toBe(true);
    expect(isKnownVisibility("Internal")).toBe(false);
    expect(isKnownVisibility("closed")).toBe(false);
  });

  it("has the same words for the levels in English", () => {
    expect(en.visibility.levels).toEqual({ public: "Public", internal: "Internal", customer: "Customer", secret: "Secret" });
    expect(en.visibility.value).toBe("Visibility: {level}");
  });
});

describe("VisibilityTag", () => {
  it("says the visitor's visibility in a square tag, with the code in the tooltip", () => {
    renderVi(<VisibilityTag level="internal" testId="visibility" />);
    const tag = screen.getByTestId("visibility");
    expect(tag).toHaveTextContent("Mức hiển thị: Internal");
    expect(tag).toHaveAttribute("title", "internal");
    expect(tag.className).toContain("rounded-xs");
    expect(tag.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});
