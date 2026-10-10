// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { hydrateRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeAll, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { setCharacterKeys } from "@/lib/keyboard";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import messages from "../../../messages/vi.json";

import { LEAD_MS, readKey, ShortcutKeys, SHORTCUTS, ShortcutsProvider, useDispatchShortcut, useOpenShortcuts } from "./shortcuts";

const nav = vi.hoisted(() => ({ project: "demo" as string | undefined, pathname: "/p/demo", push: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useParams: () => (nav.project ? { project: nav.project } : {}),
  useRouter: () => ({ push: nav.push, replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

// The shell's Dispatch is the runs' dialog; here a stand-in that says which project and step it was opened on.
vi.mock("@/components/runs/dispatch-dialog", () => ({
  DispatchDialog: ({ project, open, step }: { project: string; open: boolean; step?: string }) =>
    open ? (
      <div role="dialog" data-state="open" data-testid="dispatch-dialog">
        {project} {step ?? "any step"}
      </div>
    ) : null,
}));

beforeAll(() => {
  // A keyboard that labels its modifier Ctrl, whatever machine runs the tests.
  Object.defineProperty(window.navigator, "platform", { value: "Linux x86_64", configurable: true });
});

type Grant = { project: string; role: string; max_level: string };

const WRITER: Grant[] = [
  { project: "demo", role: "writer", max_level: "internal" },
  { project: "docs", role: "reader", max_level: "public" },
];

/** A page of the shell: a button, the fields where keys are text, the web terminal, and D's state for its own Dispatch. */
function Page({ ownDispatch }: { ownDispatch?: () => void }) {
  const dispatchKey = useDispatchShortcut(ownDispatch ?? null);
  const openShortcuts = useOpenShortcuts();
  return (
    <>
      <button type="button">Nút</button>
      <button type="button" onClick={(event) => openShortcuts?.(event.currentTarget)}>
        Mở phím tắt
      </button>
      <label>
        Lọc
        <input data-testid="field" />
      </label>
      <label>
        Ghi chú
        <textarea data-testid="note" />
      </label>
      <div contentEditable suppressContentEditableWarning tabIndex={0} data-testid="editor" />
      <div className="hub-terminal" data-testid="terminal">
        <textarea aria-label="Terminal" data-testid="terminal-input" />
      </div>
      <span data-testid="page-dispatch-key">{String(dispatchKey)}</span>
      <ShortcutKeys id="goInbox" />
    </>
  );
}

function setup({ grants = WRITER, ownDispatch, timers = false }: { grants?: Grant[]; ownDispatch?: () => void; timers?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(queryKeys.whoami, { login: "octo", admin: false, token: {}, grants });
  renderVi(
    <QueryClientProvider client={client}>
      <ShortcutsProvider>
        <Page ownDispatch={ownDispatch} />
      </ShortcutsProvider>
    </QueryClientProvider>,
  );
  return userEvent.setup(timers ? { advanceTimers: vi.advanceTimersByTime } : {});
}

beforeEach(() => {
  nav.project = "demo";
  nav.pathname = "/p/demo";
  nav.push.mockReset();
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  act(() => setCharacterKeys(true));
});

describe("go to sequences", () => {
  it("G then H, I or W goes to Home, Inbox or Workers", async () => {
    const user = setup();
    await user.keyboard("gi");
    expect(nav.push).toHaveBeenLastCalledWith("/inbox");
    await user.keyboard("gw");
    expect(nav.push).toHaveBeenLastCalledWith("/workers");
    await user.keyboard("gh");
    expect(nav.push).toHaveBeenLastCalledWith("/");
    expect(nav.push).toHaveBeenCalledTimes(3);
  });

  it("G then P or R goes to the plans or runs of the project shown", async () => {
    const user = setup();
    await user.keyboard("gp");
    expect(nav.push).toHaveBeenLastCalledWith("/p/demo/plans");
    await user.keyboard("gr");
    expect(nav.push).toHaveBeenLastCalledWith("/p/demo/runs");
  });

  it("outside a project, G P goes to the visitor's only project, or says to open one", async () => {
    nav.project = undefined;
    nav.pathname = "/";
    let user = setup({ grants: [{ project: "solo", role: "reader", max_level: "public" }] });
    await user.keyboard("gp");
    expect(nav.push).toHaveBeenLastCalledWith("/p/solo/plans");
    cleanup();

    nav.push.mockReset();
    user = setup();
    await user.keyboard("gr");
    expect(nav.push).not.toHaveBeenCalled();
    expect(await screen.findByTestId("toast-title")).toHaveTextContent("Mở một dự án trước");
  });

  it("does nothing for a second key after a second, or after another key", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = setup({ timers: true });
    await user.keyboard("g");
    act(() => vi.advanceTimersByTime(LEAD_MS + 1));
    await user.keyboard("i");
    expect(nav.push).not.toHaveBeenCalled();

    // Within the second it still works.
    await user.keyboard("g");
    act(() => vi.advanceTimersByTime(LEAD_MS - 100));
    await user.keyboard("i");
    expect(nav.push).toHaveBeenCalledWith("/inbox");

    // A key that is no second key ends the sequence.
    nav.push.mockReset();
    await user.keyboard("gxi");
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("shows the second keys when G waits for one, until it comes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = setup({ timers: true });
    await user.keyboard("g");
    expect(screen.queryByTestId("shortcuts-lead")).toBeNull();
    act(() => vi.advanceTimersByTime(500));
    const hint = screen.getByTestId("shortcuts-lead");
    expect(hint).toHaveAttribute("aria-hidden", "true");
    expect(hint).toHaveTextContent("Inbox");
    await user.keyboard("i");
    expect(screen.queryByTestId("shortcuts-lead")).toBeNull();
    expect(nav.push).toHaveBeenCalledWith("/inbox");
  });
});

describe("keys that are not shortcuts", () => {
  it("are text in an input, a text area, an editor and the web terminal", async () => {
    const user = setup();
    for (const id of ["field", "note", "terminal-input"]) {
      const field = screen.getByTestId(id);
      await user.click(field);
      await user.keyboard("gi?dgp");
      expect(field).toHaveValue("gi?dgp");
    }
    const editor = screen.getByTestId("editor");
    editor.focus();
    expect(editor).toHaveFocus();
    await user.keyboard("gi?d");
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByTestId("shortcuts-dialog")).toBeNull();
    expect(screen.queryByTestId("dispatch-dialog")).toBeNull();
  });

  it("are the terminal's even when its host holds focus", async () => {
    const user = setup();
    const terminal = screen.getByTestId("terminal");
    terminal.tabIndex = 0;
    terminal.focus();
    await user.keyboard("gi");
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("are the browser's and the screen reader's with Cmd, Ctrl or Alt held", async () => {
    const user = setup();
    await user.click(screen.getByRole("button", { name: "Nút" }));
    await user.keyboard("{Control>}g{/Control}i");
    await user.keyboard("{Meta>}g{/Meta}{Meta>}i{/Meta}");
    await user.keyboard("{Alt>}g{/Alt}{Alt>}i{/Alt}");
    await user.keyboard("{Control>}d{/Control}{Alt>}d{/Alt}");
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByTestId("dispatch-dialog")).toBeNull();
    // Shift with a letter is not the letter.
    await user.keyboard("{Shift>}g{/Shift}i");
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("are left alone while a dialog or a menu is open", async () => {
    const user = setup();
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    const item = document.createElement("div");
    item.setAttribute("role", "menuitem");
    item.tabIndex = -1;
    menu.append(item);
    document.body.append(menu);
    item.focus();
    await user.keyboard("gi");
    expect(nav.push).not.toHaveBeenCalled();
    menu.remove();

    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("data-state", "open");
    document.body.append(dialog);
    await user.keyboard("gi");
    expect(nav.push).not.toHaveBeenCalled();
    dialog.remove();
  });

  it("reads a letter by the key's place on a layout without Latin letters", () => {
    const event = { key: "п", code: "KeyG", shiftKey: false, defaultPrevented: false, isComposing: false, repeat: false, ctrlKey: false, metaKey: false, altKey: false, target: document.body };
    expect(readKey(event, false)).toEqual({ kind: "lead" });
    expect(readKey({ ...event, key: "ш", code: "KeyI" }, true)).toEqual({ kind: "go", key: "i" });
    expect(readKey({ ...event, isComposing: true }, false)).toBeNull();
    expect(readKey({ ...event, repeat: true }, false)).toBeNull();
    expect(readKey({ ...event, key: "?", code: "Slash", shiftKey: true }, false)).toEqual({ kind: "help" });
  });
});

describe("D", () => {
  it("opens the shell's Dispatch on a writer's project page that has none of its own", async () => {
    const user = setup();
    const button = screen.getByRole("button", { name: "Nút" });
    button.focus();
    await user.keyboard("d");
    const dialog = await screen.findByTestId("dispatch-dialog", {}, { timeout: 5_000 });
    expect(dialog).toHaveTextContent("demo any step");
    expect(screen.getByTestId("page-dispatch-key")).toHaveTextContent("false");
  });

  it("opens the page's own Dispatch when the page has one", async () => {
    const ownDispatch = vi.fn();
    const user = setup({ ownDispatch });
    expect(screen.getByTestId("page-dispatch-key")).toHaveTextContent("true");
    await user.keyboard("d");
    expect(ownDispatch).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("dispatch-dialog")).toBeNull();
  });

  it("is the page's own only once the page has registered it: not in the server's HTML, and from hydration on", async () => {
    // A page's content can hydrate well after the shell's: until then D opens the shell's Dispatch, so the page must
    // not show the key beside its own button yet.
    const ownDispatch = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    client.setQueryData(queryKeys.whoami, { login: "octo", admin: false, token: {}, grants: WRITER });
    const tree = (
      <NextIntlClientProvider locale="vi" messages={messages} timeZone="Asia/Ho_Chi_Minh">
        <QueryClientProvider client={client}>
          <ShortcutsProvider>
            <Page ownDispatch={ownDispatch} />
          </ShortcutsProvider>
        </QueryClientProvider>
      </NextIntlClientProvider>
    );
    let root: Root | undefined;
    const container = document.createElement("div");
    container.innerHTML = renderToString(tree);
    document.body.append(container);
    onTestFinished(() => {
      act(() => root?.unmount());
      container.remove();
    });
    const key = within(container).getByTestId("page-dispatch-key");
    expect(key).toHaveTextContent("false");

    await act(async () => {
      root = hydrateRoot(container, tree);
    });
    expect(key).toHaveTextContent("true");
    await userEvent.setup().keyboard("d");
    expect(ownDispatch).toHaveBeenCalledTimes(1);
  });

  it("does nothing for a reader, or outside a project", async () => {
    let user = setup({ grants: [{ project: "demo", role: "reader", max_level: "public" }] });
    await user.keyboard("d");
    expect(screen.queryByTestId("dispatch-dialog")).toBeNull();
    cleanup();

    nav.project = undefined;
    nav.pathname = "/workers";
    user = setup();
    await user.keyboard("d");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId("dispatch-dialog")).toBeNull();
  });
});

describe("the shortcuts dialog", () => {
  it("opens with ?, lists every key of the registry in words, and Esc gives focus back", async () => {
    const user = setup();
    const button = screen.getByRole("button", { name: "Nút" });
    button.focus();
    await user.keyboard("?");
    const dialog = await screen.findByRole("dialog", { name: "Phím tắt" });
    const rows = within(dialog).getAllByTestId("shortcut-row");
    expect(rows.map((row) => row.getAttribute("data-shortcut"))).toEqual(SHORTCUTS.map((entry) => entry.id));
    const row = (id: string) => rows.find((item) => item.getAttribute("data-shortcut") === id)!;
    expect(row("goInbox")).toHaveTextContent("G rồi I");
    expect(row("palette")).toHaveTextContent("Control K");
    expect(row("help")).toHaveTextContent("Dấu chấm hỏi");
    expect(row("goPlans")).toHaveTextContent("Của dự án đang mở");
    expect(row("dispatch")).toHaveTextContent("với vai trò Ghi");
    expect(within(dialog).getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual(["Chung", "Hành động", "Đi tới"]);

    // No key of the page works under it.
    await user.keyboard("gi");
    expect(nav.push).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(button).toHaveFocus();
  });

  it("opens from the page, and gives focus back to what opened it", async () => {
    const user = setup();
    const button = screen.getByRole("button", { name: "Mở phím tắt" });
    await user.click(button);
    expect(await screen.findByRole("dialog", { name: "Phím tắt" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Đóng" }));
    await waitFor(() => expect(button).toHaveFocus());
  });

  it("turns single keys off and on, on this browser", async () => {
    const user = setup();
    expect(document.querySelector('[data-shortcut="goInbox"]')).toHaveTextContent("GI");
    await user.keyboard("?");
    const dialog = await screen.findByRole("dialog", { name: "Phím tắt" });
    const single = within(dialog).getByRole("switch", { name: "Phím tắt một phím" });
    expect(single).toBeChecked();
    await user.click(single);
    expect(single).not.toBeChecked();
    expect(window.localStorage.getItem("hub.shortcuts.characterKeys")).toBe("off");
    const offRows = within(dialog).getAllByTestId("shortcut-row").filter((row) => row.hasAttribute("data-off"));
    expect(offRows.map((row) => row.getAttribute("data-shortcut"))).toEqual(SHORTCUTS.filter((entry) => entry.single).map((entry) => entry.id));
    expect(offRows[0]).toHaveTextContent("Đã tắt");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    // Off: no single key works, and none is shown beside its action.
    expect(document.querySelector('[data-shortcut="goInbox"]')).toBeNull();
    await user.click(screen.getByRole("button", { name: "Nút" }));
    await user.keyboard("gi?d");
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();

    act(() => setCharacterKeys(true));
    expect(window.localStorage.getItem("hub.shortcuts.characterKeys")).toBeNull();
    await user.keyboard("gi");
    expect(nav.push).toHaveBeenCalledWith("/inbox");
  });
});
