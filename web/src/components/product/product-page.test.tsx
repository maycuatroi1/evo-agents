// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import en from "../../../messages/en.json";
import viMessages from "../../../messages/vi.json";
import { CLAIMS } from "./claims";
import { docHref, GETTING_STARTED, INVITE_FORM_URL, LOCAL_GRAPH, REPO_URL } from "./links";
import { NEEDS } from "./needs";
import { ProductPage } from "./product-page";

vi.mock("next/navigation", () => ({ usePathname: () => "/about", useRouter: () => ({ push: vi.fn(), prefetch: vi.fn() }) }));

function renderPage(ui: ReactElement, locale: "en" | "vi" = "en") {
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "en" ? en : viMessages} timeZone="UTC">
      {ui}
    </NextIntlClientProvider>,
  );
}

const guest = { signInHref: "/v1/auth/web/login", member: false, hubUrl: "https://hub.acme.dev" };

for (const locale of ["en", "vi"] as const) {
  const messages = locale === "en" ? en : viMessages;
  describe(`the product page in ${locale}`, () => {
    it("links every claim to the public docs", () => {
      const { container } = renderPage(<ProductPage {...guest} />, locale);
      const claims = [...container.querySelectorAll("[data-claim]")];
      expect(new Set(claims.map((claim) => claim.getAttribute("data-claim")))).toEqual(new Set(Object.keys(CLAIMS)));
      for (const claim of claims) {
        for (const link of within(claim as HTMLElement).getAllByRole("link")) {
          expect(link.getAttribute("href")).toMatch(new RegExp(`^${REPO_URL}/blob/main/`));
        }
      }
    });

    it("shows benefits and a clearly labelled demo instead of a research report", () => {
      const { container } = renderPage(<ProductPage {...guest} />, locale);
      for (const need of NEEDS) expect(screen.getByTestId(`need-${need.key}`)).toHaveTextContent(messages.product.needs.rows[need.key].title);
      expect(screen.getByText(messages.product.hero.demo)).toBeInTheDocument();
      for (const image of within(screen.getByTestId("product-demo")).getAllByRole("img", { hidden: true })) {
        expect(image.getAttribute("src")).toMatch(/^\/product\/monitor-wide-(light|dark)\.png$/);
        expect(image).toHaveAttribute("alt", messages.product.hero.imageAlt);
      }
      expect(container.textContent).not.toMatch(/Counts as of|Số liệu tính đến|Hacker News|kg_search|10,000|10\.000/);
      expect(screen.getByTestId("product-example")).toHaveTextContent(messages.product.workflow.example);
    });

    it("offers a setup path, sign-in for existing users, and a separate local graph path", () => {
      renderPage(<ProductPage {...guest} />, locale);
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(messages.product.hero.title);
      expect(screen.getByTestId("product-get-started")).toHaveAttribute("href", "#product-start");
      expect(screen.getByTestId("product-sign-in")).toHaveAttribute("href", "/v1/auth/web/login");
      expect(screen.getByTestId("product-invite")).toHaveAttribute("href", INVITE_FORM_URL);
      expect(screen.getByTestId("product-setup-guide")).toHaveAttribute("href", docHref(GETTING_STARTED));
      expect(screen.getByTestId("product-local-guide")).toHaveAttribute("href", docHref(LOCAL_GRAPH));
      expect(screen.queryByTestId("product-open-hub")).toBeNull();
    });
  });
}

describe("the product page's integration", () => {
  it("offers a member the hub without asking them to sign in", () => {
    renderPage(<ProductPage {...guest} member />);
    expect(screen.getByTestId("product-open-hub")).toHaveAttribute("href", "/");
    expect(screen.queryByTestId("product-sign-in")).toBeNull();
    expect(screen.getByTestId("product-source")).toHaveAttribute("href", REPO_URL);
  });

  it("uses the login destination and hub origin supplied by the server", () => {
    renderPage(<ProductPage {...guest} signInHref="/login" />);
    expect(screen.getByTestId("product-sign-in")).toHaveAttribute("href", "/login");
    expect(screen.getByTestId("product-login-command")).toHaveTextContent("evo-agents hub login --url https://hub.acme.dev");
  });
});
