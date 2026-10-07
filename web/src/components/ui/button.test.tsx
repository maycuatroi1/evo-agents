// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Send } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import { Button } from "./button";

describe("Button", () => {
  it("is the ink primary by default, at the 32 px control height and 44 px under 768 px", () => {
    render(<Button>Dispatch</Button>);
    const button = screen.getByRole("button", { name: "Dispatch" });
    expect(button).toHaveAttribute("data-variant", "default");
    expect(button.className).toContain("bg-primary");
    expect(button.className).toContain("h-8");
    expect(button.className).toContain("max-md:min-h-11");
  });

  it("gives icon buttons the 44 px touch target in both directions, and keeps links in their line", () => {
    render(
      <>
        <Button size="icon" variant="ghost" aria-label="More actions for run #7" />
        <Button variant="link">Open run</Button>
      </>,
    );
    expect(screen.getByRole("button", { name: "More actions for run #7" }).className).toContain("max-md:min-w-11");
    expect(screen.getByRole("button", { name: "Open run" }).className).not.toContain("min-h-11");
  });

  it("says it is busy, turns a loader in place of its icon and ignores clicks", async () => {
    const onClick = vi.fn();
    render(
      <Button busy onClick={onClick}>
        <Send aria-hidden="true" />
        Dispatching
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Dispatching" });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button.querySelector("[data-slot=button-spinner]")).toHaveClass("animate-spin");
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("does not submit its form while busy", async () => {
    const onSubmit = vi.fn((event: SubmitEvent) => event.preventDefault());
    render(
      <form onSubmit={(event) => onSubmit(event.nativeEvent as SubmitEvent)}>
        <Button type="submit" busy>
          Sending
        </Button>
      </form>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Sending" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
