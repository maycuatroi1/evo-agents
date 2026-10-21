// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunOutput } from "@/lib/outputs";
import { renderVi } from "@/test/render";

import messages from "../../../messages/vi.json";

import { OutputStrip } from "./output-strip";
import { OutputViewer } from "./output-viewer";

function output(id: number, extra: Partial<RunOutput>): RunOutput {
  return {
    id,
    run_id: 3,
    source: "sent",
    name: `file-${id}`,
    media_type: "application/octet-stream",
    viewer: "none",
    size: 2048,
    sha256: String(id).repeat(64).slice(0, 64),
    caption: null,
    tool_call_id: null,
    created_at: "2026-10-21T10:00:00Z",
    url: `/v1/projects/northwind/runs/3/outputs/${id}/content`,
    ...extra,
  };
}

const IMAGES = [
  output(1, { name: "home.png", media_type: "image/png", viewer: "image", caption: "The home page" }),
  output(2, { name: "shots/cart.png", media_type: "image/png", viewer: "image", source: "outputs" }),
  output(3, { name: "report.md", media_type: "text/markdown", viewer: "text" }),
];

function Viewer({ outputs, first }: { outputs: RunOutput[]; first: number }) {
  const [openId, setOpenId] = useState<number | null>(first);
  return (
    <QueryClientProvider client={new QueryClient()}>
      <OutputViewer outputs={outputs} openId={openId} onOpenChange={setOpenId} />
      <span data-testid="open-id">{String(openId)}</span>
    </QueryClientProvider>
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("OutputViewer", () => {
  it("shows an image with its name, size, source and caption, and moves with the arrow keys", async () => {
    renderVi(<Viewer outputs={IMAGES} first={1} />);
    const viewer = await screen.findByTestId("output-viewer");
    expect(within(viewer).getByTestId("output-viewer-name")).toHaveTextContent("home.png");
    expect(within(viewer).getByTestId("output-viewer-meta")).toHaveTextContent(messages.outputs.source.sent);
    expect(within(viewer).getByTestId("output-image")).toHaveAttribute("src", IMAGES[0].url);
    expect(within(viewer).getByTestId("output-viewer-caption")).toHaveTextContent("The home page");
    expect(within(viewer).getByTestId("output-viewer-position")).toHaveTextContent("1 trên 3");
    expect(within(viewer).getByTestId("output-viewer-download")).toHaveAttribute("href", `${IMAGES[0].url}?download=true`);

    fireEvent.keyDown(viewer, { key: "ArrowRight" });
    expect(within(viewer).getByTestId("output-viewer-name")).toHaveTextContent("cart.png");
    expect(within(viewer).getByTestId("output-viewer-meta")).toHaveTextContent(messages.outputs.source.outputs);
    fireEvent.keyDown(viewer, { key: "ArrowLeft" });
    fireEvent.keyDown(viewer, { key: "ArrowLeft" });
    expect(within(viewer).getByTestId("output-viewer-name")).toHaveTextContent("report.md");
  });

  it("zooms an image with its buttons and keys, and starts the next image fitted", async () => {
    renderVi(<Viewer outputs={IMAGES} first={1} />);
    const viewer = await screen.findByTestId("output-viewer");
    await userEvent.click(within(viewer).getByTestId("output-viewer-zoom-in"));
    expect(within(viewer).getByTestId("output-image-box")).toHaveAttribute("data-zoom", "1.5");
    fireEvent.keyDown(viewer, { key: "+" });
    expect(within(viewer).getByTestId("output-image-box")).toHaveAttribute("data-zoom", "2");
    fireEvent.keyDown(viewer, { key: "0" });
    expect(within(viewer).getByTestId("output-image-box")).toHaveAttribute("data-zoom", "1");
    fireEvent.keyDown(viewer, { key: "+" });
    fireEvent.keyDown(viewer, { key: "ArrowRight" });
    expect(within(viewer).getByTestId("output-image-box")).toHaveAttribute("data-zoom", "1");
  });

  it("closes with Esc", async () => {
    renderVi(<Viewer outputs={IMAGES} first={2} />);
    await screen.findByTestId("output-viewer");
    await userEvent.keyboard("{Escape}");
    expect(screen.getByTestId("open-id")).toHaveTextContent("null");
  });

  it("frames HTML in a sandbox that lets scripts run without the hub's origin", async () => {
    const page = output(4, { name: "chart.html", media_type: "text/html", viewer: "html" });
    renderVi(<Viewer outputs={[page]} first={4} />);
    const frame = await screen.findByTestId("output-html");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts");
    expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(frame).toHaveAttribute("src", page.url);
    expect(screen.queryByTestId("output-viewer-position")).not.toBeInTheDocument();
  });

  it("reads text from the hub and draws Markdown, JSON and CSV as such", async () => {
    const bodies: Record<string, string> = {
      "/v1/projects/northwind/runs/3/outputs/5/content": "# Findings\n\nAll **good**.",
      "/v1/projects/northwind/runs/3/outputs/6/content": '{"pass":3,"fail":0}',
      "/v1/projects/northwind/runs/3/outputs/7/content": "name,count\nlumen,2\n",
    };
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => new Response(bodies[url] ?? "", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const texts = [
      output(5, { name: "notes.md", media_type: "text/markdown", viewer: "text", size: 30 }),
      output(6, { name: "result.json", media_type: "application/json", viewer: "text", size: 20 }),
      output(7, { name: "counts.csv", media_type: "text/csv", viewer: "text", size: 20 }),
    ];
    renderVi(<Viewer outputs={texts} first={5} />);
    expect(await screen.findByRole("heading", { name: "Findings" })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByTestId("output-viewer"), { key: "ArrowRight" });
    await waitFor(() => expect(screen.getByTestId("output-pre")).toHaveTextContent('"pass": 3'));
    fireEvent.keyDown(screen.getByTestId("output-viewer"), { key: "ArrowRight" });
    const table = await screen.findByTestId("output-csv");
    expect(within(table).getByRole("columnheader", { name: "name" })).toBeInTheDocument();
    expect(within(table).getByRole("cell", { name: "lumen" })).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "same-origin" });
  });

  it("asks for only the start of a long text file and says so", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response("line\n".repeat(10), { status: 206 }));
    vi.stubGlobal("fetch", fetchMock);
    renderVi(<Viewer outputs={[output(8, { name: "big.log", media_type: "text/plain", viewer: "text", size: 5 * 1024 * 1024 })]} first={8} />);
    expect(await screen.findByTestId("output-text-cut")).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ headers: { Range: "bytes=0-1048575" } });
  });

  it("offers a download for a kind it cannot show", async () => {
    const archive = output(9, { name: "bundle.zip", media_type: "application/zip" });
    renderVi(<Viewer outputs={[archive]} first={9} />);
    const none = await screen.findByTestId("output-no-preview");
    expect(within(none).getByRole("link")).toHaveAttribute("href", `${archive.url}?download=true`);
  });
});

describe("OutputStrip", () => {
  it("lists the files as buttons named for what they open, the rest behind one button", async () => {
    const many = Array.from({ length: 10 }, (_, index) => output(index + 1, { name: `shot-${index + 1}.png`, media_type: "image/png", viewer: "image" }));
    renderVi(<OutputStrip outputs={many} label="Files" onOpen={() => {}} />);
    const strip = screen.getByRole("list", { name: "Files" });
    expect(within(strip).getAllByTestId("output-chip")).toHaveLength(8);
    expect(within(strip).getAllByTestId("output-chip")[0]).toHaveAccessibleName(/Mở shot-1\.png, Ảnh, 2 kB/);
    expect(within(strip).getByTestId("output-strip-more")).toHaveTextContent("thêm 2 file");
  });

  it("draws nothing without files", () => {
    renderVi(<OutputStrip outputs={[]} label="Files" />);
    expect(screen.queryByTestId("output-strip")).not.toBeInTheDocument();
  });
});
