// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Route } from "next";
import { describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { Identifier, RunRef, Tag } from "./identifier";

describe("Identifier", () => {
  it("shows a name in a mono chip with 4 px corners, as text or as a link", () => {
    renderVi(
      <>
        <Identifier value="feat/hub-ui-kit" />
        <Identifier value="binhna-macbook-m4" href={"/workers/3" as Route} />
      </>,
    );
    const chip = screen.getByText("feat/hub-ui-kit").parentElement;
    expect(chip?.className).toContain("rounded-xs");
    expect(chip?.className).toContain("font-mono");
    expect(screen.getByRole("link", { name: "binhna-macbook-m4" })).toHaveAttribute("href", "/workers/3");
  });

  it("copies the whole value while showing a short one, and says so", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const sha = "4f2c9d1e0a7b3c5d6e8f9a0b1c2d3e4f5a6b7c8d";
    renderVi(
      <Identifier value={sha} title={sha} copy copyLabel="Sao chép SHA của commit">
        4f2c9d1
      </Identifier>,
    );
    await user.click(screen.getByRole("button", { name: "Sao chép SHA của commit" }));
    expect(writeText).toHaveBeenCalledWith(sha);
    expect(await screen.findByText(`Đã chép ${sha} vào clipboard`)).toBeInTheDocument();
  });
});

describe("RunRef and Tag", () => {
  it("writes a run as #N, linked with its own name when it has a page", () => {
    renderVi(<RunRef id={7} href={"/p/demo/runs/7" as Route} label="Mở run #7" data-run-id={7} />);
    const link = screen.getByRole("link", { name: "Mở run #7" });
    expect(link).toHaveTextContent("#7");
    expect(link).toHaveAttribute("data-run-id", "7");
  });

  it("names a kind or a role in a square-cornered tag", () => {
    renderVi(<Tag data-role="admin">Quản trị</Tag>);
    expect(screen.getByText("Quản trị").className).toContain("rounded-xs");
  });
});
