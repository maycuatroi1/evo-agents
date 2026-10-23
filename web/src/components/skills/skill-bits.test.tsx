// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import { AudienceBadge } from "./skill-bits";

describe("AudienceBadge", () => {
  it("tags a skill for hub admins alone with an icon and a word, never colour alone", () => {
    renderVi(<AudienceBadge audience="admins" />);
    const tag = screen.getByTestId("skill-audience-admins");
    expect(tag).toHaveTextContent("Chỉ quản trị viên hub");
    expect(tag).toHaveAttribute("title", "Chỉ quản trị viên hub thấy, tải về và đồng bộ skill này");
    expect(tag.querySelector("svg[aria-hidden='true']")).not.toBeNull();
  });

  it("adds nothing to a skill every member reads", () => {
    const { container } = renderVi(<AudienceBadge audience="everyone" />);
    expect(screen.queryByTestId("skill-audience-admins")).toBeNull();
    expect(container.querySelector("[data-slot='tag']")).toBeNull();
  });
});
