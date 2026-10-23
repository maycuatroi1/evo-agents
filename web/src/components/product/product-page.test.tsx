// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import messages from "../../../messages/en.json";
import { CLAIMS } from "./claims";
import { INVITE_FORM_URL, REPO_URL } from "./links";
import { NEEDS } from "./needs";
import { ProductPage } from "./product-page";

vi.mock("next/navigation", () => ({ usePathname: () => "/about", useRouter: () => ({ push: vi.fn(), prefetch: vi.fn() }) }));

function renderEn(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages} timeZone="UTC">
      {ui}
    </NextIntlClientProvider>,
  );
}

const guest = { signInHref: "/v1/auth/web/login", member: false, hubUrl: "https://hub.acme.dev" };

describe("the product page", () => {
  it("links each claim to the docs of the public repository", () => {
    const { container } = renderEn(<ProductPage {...guest} />);
    const claims = [...container.querySelectorAll("[data-claim]")];
    expect(new Set(claims.map((claim) => claim.getAttribute("data-claim")))).toEqual(new Set(Object.keys(CLAIMS)));
    for (const claim of claims) {
      const links = within(claim as HTMLElement).getAllByRole("link");
      expect(links.length, claim.getAttribute("data-claim") ?? "").toBeGreaterThan(0);
      for (const link of links) expect(link.getAttribute("href")).toMatch(new RegExp(`^${REPO_URL}/blob/main/`));
    }
  });

  it("shows each need with its sources and a screen of the demo hub from this origin", () => {
    renderEn(<ProductPage {...guest} />);
    for (const need of NEEDS) {
      const row = screen.getByTestId(`need-${need.key}`);
      for (const source of need.sources) expect(within(row).getByRole("link", { name: source.title })).toHaveAttribute("href", source.url);
      for (const image of within(row).getAllByRole("img", { hidden: true }).filter((node) => node.tagName === "IMG")) {
        expect(image.getAttribute("src")).toMatch(/^\/product\/[a-z-]+-wide-(light|dark)\.png$/);
        expect(image).toHaveAttribute("alt", messages.product.needs.rows[need.key].imageAlt);
      }
    }
    expect(screen.getAllByText("Counts as of Oct 23, 2026.").length).toBe(NEEDS.length);
  });

  it("offers a guest Sign in with GitHub and the invite form, and a member the hub", () => {
    const { unmount } = renderEn(<ProductPage {...guest} />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(messages.product.hero.title);
    expect(screen.getByRole("link", { name: "Sign in with GitHub" })).toHaveAttribute("href", "/v1/auth/web/login");
    expect(screen.getByRole("link", { name: "Ask for an invite" })).toHaveAttribute("href", INVITE_FORM_URL);
    expect(screen.queryByRole("link", { name: "Open the hub" })).toBeNull();
    expect(within(screen.getByRole("navigation", { name: "evo-agents on GitHub" })).getByRole("link", { name: "Source code" })).toHaveAttribute(
      "href",
      REPO_URL,
    );
    unmount();
    renderEn(<ProductPage {...guest} member />);
    expect(screen.getByRole("link", { name: "Open the hub" })).toHaveAttribute("href", "/");
    expect(screen.queryByRole("link", { name: "Sign in with GitHub" })).toBeNull();
  });

  it("gives the CLI's sign-in the hub it is served from", () => {
    renderEn(<ProductPage {...guest} />);
    expect(screen.getByTestId("product-login-command")).toHaveTextContent("evo-agents hub login --url https://hub.acme.dev");
  });
});
