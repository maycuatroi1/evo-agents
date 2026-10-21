// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pluginKeys } from "@/lib/plugin-queries";
import { queryKeys } from "@/lib/queries";
import { documentOf, ITEMS } from "@/test/fixtures/srs";
import { renderVi } from "@/test/render";

import { srsKeys } from "./api";
import { DocumentPage } from "./document-page";
import { ItemBody } from "./item-body";
import { itemHref } from "./links";
import { ReviewForm } from "./review-form";
import { ReviewPage } from "./review-page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/demo/x/srs/documents/demo-shop",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "demo";
const links = { item: (ref: string) => itemHref(PROJECT, "demo-shop", ref), section: (key: string) => itemHref(PROJECT, "demo-shop", key) };

function withClient(ui: ReactElement, client: QueryClient) {
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/** A client that holds the document, its comments and the visitor, and never asks the API. */
function seeded(role: "reader" | "writer") {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  const doc = documentOf();
  client.setQueryData(srsKeys.version(PROJECT, "demo-shop", null), doc);
  client.setQueryData(srsKeys.version(PROJECT, "demo-shop", 2), { ...doc, shown: { ...doc.shown!, ...doc.versions[1] } });
  client.setQueryData(srsKeys.comments(PROJECT, "demo-shop"), []);
  client.setQueryData(queryKeys.whoami, {
    login: "bao",
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role, max_level: "internal" }],
  });
  return client;
}

/** jsdom has no ResizeObserver; Radix's radio group measures its items with one. */
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no API in unit tests"))));
});
afterEach(() => vi.unstubAllGlobals());

describe("ItemBody", () => {
  it("draws a use case's fields, Activity flow with its codes as links, its business rules and screens", () => {
    const useCase = ITEMS.find((one) => one.key === "uc1")!;
    renderVi(<ItemBody items={ITEMS} item={useCase} links={links} />);
    const facts = screen.getByTestId("srs-facts");
    expect(within(facts).getByText("Mục tiêu")).toBeInTheDocument();
    expect(within(facts).getByText("The customer turns a cart into an order")).toBeInTheDocument();
    expect(within(facts).getByText("Priority")).toBeInTheDocument();
    const steps = screen.getByTestId("srs-steps");
    expect(within(steps).getAllByRole("row")).toHaveLength(3);
    expect(within(steps).getByRole("link", { name: "BR ORD1-1" })).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/items/BR%20ORD1-1");
    expect(screen.getByTestId("srs-rule")).toHaveTextContent("Cart rules");
    expect(within(screen.getByTestId("srs-screens")).getByText("Cart")).toBeInTheDocument();
    expect(screen.getByText("The cart is kept for a day.")).toBeInTheDocument();
  });

  it("draws a table row as its cells under its section's headers", () => {
    const row = ITEMS.find((one) => one.key === "nfr1")!;
    renderVi(<ItemBody items={ITEMS} item={row} links={links} />);
    const facts = screen.getByTestId("srs-facts");
    expect(within(facts).getByText("Group")).toBeInTheDocument();
    expect(within(facts).getByText("Security")).toBeInTheDocument();
  });
});

describe("DocumentPage", () => {
  it("lets a writer edit and shows a reader the approved version, read only", () => {
    const asked = { version: null, item: "uc1", edit: false };
    const writer = withClient(<DocumentPage project={PROJECT} slug="demo-shop" asked={asked} initialError={null} />, seeded("writer"));
    expect(screen.getByTestId("srs-item-title")).toHaveTextContent("Place an order");
    expect(screen.getByTestId("srs-edit")).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop?v=3&s=uc1&edit=1");
    expect(screen.queryByTestId("srs-read-only")).toBeNull();
    expect(screen.getByTestId("srs-version-menu")).toHaveTextContent("v2");
    writer.unmount();

    withClient(<DocumentPage project={PROJECT} slug="demo-shop" asked={asked} initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("srs-read-only")).toBeInTheDocument();
    expect(screen.queryByTestId("srs-edit")).toBeNull();
    expect(screen.getByTestId("srs-item-page")).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/items/UC%20ORD-1");
  });
});

describe("ReviewForm", () => {
  it("asks for a note before it sends a request for changes", async () => {
    const user = userEvent.setup();
    const doc = documentOf();
    withClient(<ReviewForm project={PROJECT} slug="demo-shop" version={doc.versions[0]} roles={["qa", "dev"]} />, seeded("writer"));
    expect(screen.getByTestId("srs-review-role")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /Yêu cầu sửa/ }));
    await user.click(screen.getByTestId("srs-review-submit"));
    expect(screen.getByRole("alert")).toHaveTextContent("Hãy ghi cần sửa gì.");
    expect(screen.getByTestId("srs-review-note")).toHaveAttribute("aria-invalid", "true");
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("ReviewPage", () => {
  /** The client of `seeded`, signed in as alice, the author of v3 in review, with the config's `self_approval`. */
  function asAuthor(selfApproval: boolean) {
    const client = seeded("writer");
    client.setQueryData(queryKeys.whoami, {
      login: "alice",
      admin: false,
      token: { kind: "web", scopes: [], expires_at: null },
      grants: [{ project: PROJECT, role: "writer", max_level: "internal" }],
    });
    const field = { name: "self_approval", value: selfApproval, secret: false, layer: selfApproval ? "project" : "default", version: selfApproval ? 1 : null };
    client.setQueryData(pluginKeys.effective(PROJECT, "srs"), { plugin: "srs", version: "1.0.0", project: PROJECT, agent: "default", fields: [field], problems: [], digest: "d" });
    return client;
  }

  it("tells the author they cannot review their version, and gives them the form once self-approval is on", () => {
    const page = <ReviewPage project={PROJECT} slug="demo-shop" number={3} initialError={null} />;
    const off = withClient(page, asAuthor(false));
    expect(screen.getByTestId("srs-review-blocked")).toHaveTextContent("Bạn không duyệt được v3");
    expect(screen.getByTestId("srs-review-blocked")).toHaveTextContent("Bạn đã viết v3 kể từ v2");
    expect(screen.queryByTestId("srs-self-approval")).toBeNull();
    expect(screen.queryByTestId("srs-review-submit")).toBeNull();
    off.unmount();

    withClient(page, asAuthor(true));
    expect(screen.queryByTestId("srs-review-blocked")).toBeNull();
    expect(screen.getByTestId("srs-review-submit")).toBeInTheDocument();
    expect(screen.getByTestId("srs-self-approval")).toHaveTextContent("Dự án demo đã bật tự duyệt");
    expect(screen.getByTestId("srs-approvals")).toHaveTextContent("kể cả lượt của chính người viết");
  });
});
