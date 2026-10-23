import type { Locator, Page } from "@playwright/test";

import { STACK_URL } from "./env";

/**
 * The worker's end of a run's live browser view, from the stack's control port (e2e/hub_stack.py, FakeBrowser in
 * e2e/fake_browser.py): it connects with the worker's token as the daemon's broker does, shows two tabs whose screens
 * are stills of one colour (northwind blue, lumen amber), takes its mode from the run's state, and records every event
 * and ACK the page sends.
 */
export type FakeBrowserEvent = { type: string } & Record<string, unknown>;

export type FakeBrowserState = {
  connected: boolean;
  /** How many times its end attached to the hub. */
  attaches: number;
  refused: (number | string | null)[];
  closed: number | string | null;
  mode: "agent" | "human";
  active: string;
  /** Each VIEW the hub sent: someone watching, or no longer. */
  views: { viewing: boolean; login: boolean }[];
  /** Each SCREEN it sent: its seq and the tab it showed. */
  screens: { seq: number; tab: string }[];
  /** The seq of each ACK, in order. */
  acks: number[];
  /** Every INPUT event, as the hub passed it. */
  inputs: FakeBrowserEvent[];
  /** The notices its STATE carried. */
  notices: string[];
};

/** The tabs the fake starts with. */
export const NORTHWIND = { id: "northwind", url: "https://northwind.example/orders", title: "Northwind orders" };
export const LUMEN = { id: "lumen", url: "https://lumen.example/", title: "Lumen" };
/** The page each SCREEN header measures, in CSS pixels. */
export const PAGE_SIZE = { width: 1280, height: 800 };

async function control<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${STACK_URL}${path}`, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`hub_stack ${path}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

/** Connect the worker's end of run `runId`'s browser. */
export async function startFakeBrowser(runId: number, workerToken: string): Promise<void> {
  await control("POST", "/browser/worker", { run_id: runId, token: workerToken });
}

export function fakeBrowser(runId: number): Promise<FakeBrowserState> {
  return control("GET", `/browser/worker/${runId}`);
}

/** drop: the worker's end goes away (the view waits); resume: it connects again; close: the run's browser closes. */
export async function endFakeBrowser(runId: number, how: "drop" | "resume" | "close"): Promise<void> {
  await control("POST", "/browser/worker/end", { run_id: runId, how });
}

/** The events of `type` the fake got, in order. */
export async function inputsOf(runId: number, type: string): Promise<FakeBrowserEvent[]> {
  return (await fakeBrowser(runId)).inputs.filter((event) => event.type === type);
}

/** The colour of the canvas's middle pixel, as the page drew it. */
export async function middleColour(canvas: Locator): Promise<[number, number, number]> {
  return canvas.evaluate((element: HTMLCanvasElement) => {
    const context = element.getContext("2d");
    if (!context || element.width === 0) return [0, 0, 0] as [number, number, number];
    const [r, g, b] = context.getImageData(Math.floor(element.width / 2), Math.floor(element.height / 2), 1, 1).data;
    return [r, g, b] as [number, number, number];
  });
}

/** Which of the fake's stills the canvas shows: blue is northwind's, amber lumen's. */
export async function shownTab(canvas: Locator): Promise<"northwind" | "lumen" | null> {
  const [r, , b] = await middleColour(canvas);
  if (b > r + 80) return "northwind";
  if (r > b + 80) return "lumen";
  return null;
}

/** A websocket to run `runId`'s browser opened by the page itself, with the session's CSRF value: its close code. */
export async function browserCloseCode(page: Page, project: string, runId: number): Promise<{ code: number; reason: string }> {
  return page.evaluate(
    async ({ project, runId }) => {
      const { csrf } = (await (await fetch("/v1/auth/web/csrf")).json()) as { csrf: string };
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(`${scheme}//${window.location.host}/v1/projects/${encodeURIComponent(project)}/runs/${runId}/browser`);
      return new Promise<{ code: number; reason: string }>((resolve) => {
        socket.onopen = () => socket.send(JSON.stringify({ csrf }));
        socket.onclose = (event) => resolve({ code: event.code, reason: event.reason });
      });
    },
    { project, runId },
  );
}

/** Where the canvas draws the page, on the window: the box its picture fills, letterboxed (object-fit: contain). */
export async function pictureBox(canvas: Locator): Promise<{ left: number; top: number; width: number; height: number }> {
  return canvas.evaluate((element: HTMLCanvasElement) => {
    const box = element.getBoundingClientRect();
    const scale = Math.min(box.width / element.width, box.height / element.height);
    const width = element.width * scale;
    const height = element.height * scale;
    return { left: box.left + (box.width - width) / 2, top: box.top + (box.height - height) / 2, width, height };
  });
}
