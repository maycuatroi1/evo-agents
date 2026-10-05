import type { Page } from "@playwright/test";

import { STACK_URL } from "./env";

/**
 * The worker's end of a run's terminal and the hub's 12-hour limit, from the stack's control port (e2e/hub_stack.py):
 * a fake daemon that connects its end with the worker's token once a browser waits, over a real PTY that echoes what
 * is typed and answers each line with `echo: <line>` and each resize with `size: <cols>x<rows>`.
 */
export type FakeTerminalState = {
  connected: boolean;
  /** Close codes (or errors) of the tries made while no browser waited. */
  refused: (number | string | null)[];
  /** The close code once the session ended. */
  closed: number | string | null;
  /** Every size the worker got, first from the browser's hello. */
  sizes: [number, number][];
  /** The lines the PTY's program read. */
  lines: string[];
};

async function control<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${STACK_URL}${path}`, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`hub_stack ${path}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

/** Start the worker's end of run `runId`'s terminal; it keeps trying for 30 seconds while no browser waits. */
export async function startFakeTerminal(runId: number, workerToken: string): Promise<void> {
  await control("POST", "/terminal/worker", { run_id: runId, token: workerToken });
}

export function fakeTerminal(runId: number): Promise<FakeTerminalState> {
  return control("GET", `/terminal/worker/${runId}`);
}

/** Make `login`'s web sessions `hours` older. */
export async function ageSessions(login: string, hours: number): Promise<number> {
  const { sessions } = await control<{ sessions: number }>("POST", "/sessions/age", { login, hours });
  return sessions;
}

/**
 * Content Security Policy violations of `page` from its first load: the `securitypolicyviolation` events of every
 * document, and the console errors Chromium ("Refused to ...") and Firefox ("Content-Security-Policy: ...") log for
 * them. Firefox's warnings about sources a policy ignores (such as 'self' beside 'strict-dynamic') block nothing and
 * are left out.
 */
export async function watchCsp(page: Page): Promise<string[]> {
  const violations: string[] = [];
  await page.exposeBinding("__reportCspViolation", (_source, text: string) => {
    violations.push(text);
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      const report = (window as unknown as { __reportCspViolation?: (text: string) => void }).__reportCspViolation;
      report?.(`${event.effectiveDirective} blocked ${event.blockedURI}`);
    });
  });
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    if (/content.security.policy|refused to (connect|load|execute|compile|evaluate)/i.test(message.text())) violations.push(message.text());
  });
  return violations;
}

/** A websocket to run `runId`'s terminal opened by the page itself, with the session's CSRF value: its close code. */
export async function terminalCloseCode(page: Page, project: string, runId: number): Promise<{ code: number; reason: string }> {
  return page.evaluate(
    async ({ project, runId }) => {
      const { csrf } = (await (await fetch("/v1/auth/web/csrf")).json()) as { csrf: string };
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(`${scheme}//${window.location.host}/v1/projects/${encodeURIComponent(project)}/runs/${runId}/terminal`);
      return new Promise<{ code: number; reason: string }>((resolve) => {
        socket.onopen = () => socket.send(JSON.stringify({ csrf, cols: 80, rows: 24 }));
        socket.onclose = (event) => resolve({ code: event.code, reason: event.reason });
      });
    },
    { project, runId },
  );
}
