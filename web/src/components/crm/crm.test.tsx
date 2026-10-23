// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { type CrmContactDetail, type CrmContacts, contactHref, crmKeys } from "./api";
import { ContactPage } from "./contact-page";
import { ContactsPage } from "./contacts-page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/demo/x/crm/contacts",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "demo";
const AT = "2026-10-20T03:00:00Z";

function contact(login: string, stage: string, source = "issue") {
  return { login, profile_url: `https://github.com/${login}`, source, stage, first_seen_at: AT, stage_at: AT };
}

const LIST: CrmContacts = {
  project: PROJECT,
  counts: { lead: 2, invited: 1, activated: 0, retained: 1 },
  contacts: [contact("ann", "lead"), contact("bao", "lead", "invite_request"), contact("chi", "invited", "invite_link"), contact("dung", "retained", "grant")],
};

const DETAIL: CrmContactDetail = {
  ...contact("chi", "invited", "invite_link"),
  touchpoints: [
    { kind: "invite_link", ref: "invite:3", url: null, note: null, author: null, happened_at: AT },
    { kind: "note", ref: "n1", url: null, note: "Met at the meetup", author: "bao", happened_at: "2026-10-21T03:00:00Z" },
  ],
  history: [
    { stage: "lead", reached_at: AT },
    { stage: "invited", reached_at: AT },
  ],
};

function seeded(role: "reader" | "writer" | "admin") {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(crmKeys.contacts(PROJECT), LIST);
  client.setQueryData(crmKeys.contact(PROJECT, "chi"), DETAIL);
  client.setQueryData(queryKeys.whoami, {
    login: "bao",
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role, max_level: "internal" }],
  });
  return client;
}

function withClient(ui: ReactElement, client: QueryClient) {
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("the contacts page", () => {
  it("counts each stage in the pipeline and filters the list by the stage pressed", () => {
    withClient(<ContactsPage project={PROJECT} title="Contacts" initialError={null} />, seeded("writer"));
    const pipeline = screen.getByTestId("crm-pipeline");
    expect(within(screen.getByTestId("crm-stage-all")).getByTestId("crm-stage-count")).toHaveTextContent("4");
    expect(within(screen.getByTestId("crm-stage-lead")).getByTestId("crm-stage-count")).toHaveTextContent("2");
    expect(within(pipeline).getAllByRole("button")).toHaveLength(5);
    expect(screen.getAllByTestId("crm-contact-row")).toHaveLength(4);
    expect(screen.getAllByTestId("crm-contact-link")[0]).toHaveAttribute("href", "/p/demo/x/crm/contacts/ann");

    fireEvent.click(screen.getByTestId("crm-stage-invited"));
    expect(screen.getByTestId("crm-stage-invited")).toHaveAttribute("aria-pressed", "true");
    const rows = screen.getAllByTestId("crm-contact-row");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("chi");
    expect(rows[0]).toHaveTextContent("Liên kết mời");

    fireEvent.click(screen.getByTestId("crm-stage-activated"));
    expect(screen.getByTestId("crm-empty")).toHaveTextContent("Đã kích hoạt");
    expect(screen.queryByTestId("crm-read-only")).toBeNull();
  });

  it("tells a reader they only read", () => {
    withClient(<ContactsPage project={PROJECT} title="Contacts" initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("crm-read-only")).toBeInTheDocument();
  });
});

describe("a contact's page", () => {
  it("shows the touchpoints newest first, the stages reached, and a writer's note but no Delete", () => {
    withClient(<ContactPage project={PROJECT} login="chi" initialError={null} />, seeded("writer"));
    const points = screen.getAllByTestId("crm-touchpoint");
    expect(points[0]).toHaveTextContent("Met at the meetup");
    expect(points[0]).toHaveTextContent("bao");
    expect(points[1]).toHaveTextContent("invite:3");
    expect(screen.getAllByTestId("crm-history-step")).toHaveLength(2);
    expect(screen.getByTestId("crm-note-form")).toBeInTheDocument();
    expect(screen.queryByTestId("crm-delete")).toBeNull();
    expect(screen.getByTestId("crm-back")).toHaveAttribute("href", "/p/demo/x/crm/contacts");
  });

  it("gives an admin Delete behind a confirmation, and a reader neither the note nor Delete", () => {
    const { unmount } = withClient(<ContactPage project={PROJECT} login="chi" initialError={null} />, seeded("admin"));
    fireEvent.click(screen.getByTestId("crm-delete"));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("Xóa chi?");
    expect(screen.getByTestId("crm-delete-confirm-confirm")).toBeInTheDocument();
    unmount();

    withClient(<ContactPage project={PROJECT} login="chi" initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("crm-read-only")).toBeInTheDocument();
    expect(screen.queryByTestId("crm-note-form")).toBeNull();
    expect(screen.queryByTestId("crm-delete")).toBeNull();
  });

  it("encodes a login in its link", () => {
    expect(contactHref("demo", "a-b")).toBe("/p/demo/x/crm/contacts/a-b");
  });
});
