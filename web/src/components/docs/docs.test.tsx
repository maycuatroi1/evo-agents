// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SafeMarkdown } from "@/components/data/markdown";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { type DocsDocument, docsKeys } from "./api";
import { DocumentPage } from "./document-page";
import { imageKey, imageResolver, shownImages, slugOf, uploadName, versionParam } from "./model";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/demo/x/docs/documents/guide",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "demo";
const SHA = "a".repeat(64);
const URL_OF_BOARD = `/v1/x/docs/demo/documents/guide/images/${SHA}`;
const BODY = '## 1. Vào hub\n\n![Trang Tasks](shots/board.png "Apps > Tasks")\n\n![Ngoài](https://example.org/x.png)\n';

function documentOf(shownNumber = 2): DocsDocument {
  const version = (number: number) => ({
    number,
    title: "Hướng dẫn",
    summary: number === 1 ? "bản đầu" : "",
    author: "alice",
    created_at: `2026-10-2${number}T03:00:00Z`,
  });
  return {
    slug: "guide",
    title: "Hướng dẫn",
    latest_version: 2,
    label: { level: "customer" },
    created_by: "alice",
    created_at: "2026-10-21T03:00:00Z",
    updated_by: "alice",
    updated_at: "2026-10-22T03:00:00Z",
    versions: [version(2), version(1)],
    shown: {
      ...version(shownNumber),
      body: BODY,
      images: [{ name: "shots/board.png", sha256: SHA, media_type: "image/png", size: 10, url: URL_OF_BOARD }],
    },
  };
}

function seeded(role: "reader" | "writer", shownNumber: number | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(docsKeys.version(PROJECT, "guide", shownNumber), documentOf(shownNumber ?? 2));
  client.setQueryData(queryKeys.whoami, {
    login: "bao",
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role, max_level: "customer" }],
  });
  return client;
}

function withClient(ui: ReactElement, client: QueryClient) {
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no API in unit tests"))));
});
afterEach(() => vi.unstubAllGlobals());

describe("the rules of the Docs pages", () => {
  it("finds an image a version holds by the source the Markdown gives it, and nothing else", () => {
    const resolve = imageResolver([{ name: "shots/board.png", url: URL_OF_BOARD }]);
    expect(resolve("shots/board.png")).toBe(URL_OF_BOARD);
    expect(resolve("./shots/board.png")).toBe(URL_OF_BOARD);
    expect(resolve("shots/board%2Epng")).toBe(URL_OF_BOARD);
    expect(resolve("https://example.org/shots/board.png")).toBeNull();
    expect(resolve("/shots/board.png")).toBeNull();
    expect(resolve("shots/other.png")).toBeNull();
    expect(imageKey("%E0%A4%A")).toBeNull();
  });

  it("keeps the images the Markdown shows, once each, in order", () => {
    const known = new Set(["images/a.png", "images/b.png", "images/c.png"]);
    const body = "![b](images/b.png)\n![a](<./images/a.png>)\n![b again](images/b.png \"t\")\n![x](images/x.png)";
    expect(shownImages(body, known)).toEqual(["images/b.png", "images/a.png"]);
  });

  it("names an upload under images/, cleaned and unique", () => {
    expect(uploadName("Ảnh board 1.png", new Set())).toBe("images/nh-board-1.png");
    expect(uploadName("board.png", new Set(["images/board.png"]))).toBe("images/board-2.png");
    expect(uploadName("...", new Set())).toBe("images/image");
  });

  it("makes a slug of a Vietnamese title and reads a version from the address", () => {
    expect(slugOf("Hướng dẫn điều phối việc của nhóm")).toBe("huong-dan-dieu-phoi-viec-cua-nhom");
    expect(versionParam("3")).toBe(3);
    expect(versionParam(["2", "5"])).toBe(2);
    for (const bad of [undefined, "", "0", "-1", "1.5", "x"]) expect(versionParam(bad)).toBeNull();
  });
});

describe("SafeMarkdown with images", () => {
  it("shows an image the page holds with its caption, and leaves every other a placeholder", () => {
    const resolve = imageResolver([{ name: "shots/board.png", url: URL_OF_BOARD }]);
    renderVi(<SafeMarkdown images={resolve} variant="page">{BODY}</SafeMarkdown>);
    const image = screen.getByRole("img", { name: "Trang Tasks" });
    expect(image).toHaveAttribute("src", URL_OF_BOARD);
    expect(within(screen.getByTestId("markdown-image")).getByText("Apps > Tasks")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Ngoài" })).toBeNull();
    expect(screen.getByText("Hình: Ngoài")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "1. Vào hub" })).toBeInTheDocument();
  });

  it("loads no image without a resolver, as memories and chats render", () => {
    renderVi(<SafeMarkdown>{BODY}</SafeMarkdown>);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("heading", { level: 4, name: "1. Vào hub" })).toBeInTheDocument();
  });
});

describe("DocumentPage", () => {
  it("draws the latest version with its images, and Edit for a writer", () => {
    withClient(<DocumentPage project={PROJECT} slug="guide" version={null} initialError={null} />, seeded("writer"));
    expect(screen.getByRole("heading", { level: 1, name: "Hướng dẫn" })).toBeInTheDocument();
    expect(screen.getByTestId("docs-document-head")).toHaveTextContent("Version 2 trên 2, alice lưu");
    expect(screen.getByRole("img", { name: "Trang Tasks" })).toHaveAttribute("src", URL_OF_BOARD);
    expect(screen.getByTestId("docs-edit")).toHaveAttribute("href", "/p/demo/x/docs/documents/guide/edit");
    expect(screen.queryByTestId("docs-older-version")).toBeNull();
  });

  it("says when a reader reads an older version, with no Edit", () => {
    withClient(<DocumentPage project={PROJECT} slug="guide" version={1} initialError={null} />, seeded("reader", 1));
    expect(screen.getByTestId("docs-older-version")).toHaveTextContent("Bạn đang đọc version 1; bản mới nhất là version 2.");
    expect(screen.queryByTestId("docs-edit")).toBeNull();
  });
});
