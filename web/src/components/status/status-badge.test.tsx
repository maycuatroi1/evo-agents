// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import en from "../../../messages/en.json";
import vi from "../../../messages/vi.json";

import { OtherStatusBadge, STATUS_LOOKS, StatusBadge, type StatusKind, type StatusLook } from "./status-badge";

const MESSAGES = { en, vi } as const;
type Locale = keyof typeof MESSAGES;

function renderIn(locale: Locale, ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale={locale} messages={MESSAGES[locale]} timeZone="Asia/Ho_Chi_Minh">
      {ui}
    </NextIntlClientProvider>,
  );
}

const KINDS = Object.keys(STATUS_LOOKS) as StatusKind[];

/** Every kind with each of its states, as the tables declare them. */
const CASES = KINDS.flatMap((kind) => Object.keys(STATUS_LOOKS[kind]).map((status) => [kind, status] as const));

function lookOf(kind: StatusKind, status: string): StatusLook {
  return (STATUS_LOOKS[kind] as Record<string, StatusLook>)[status];
}

function words(locale: Locale, kind: StatusKind, status: string): string {
  return (MESSAGES[locale].status[kind] as Record<string, string>)[status];
}

describe("StatusBadge", () => {
  it("covers the kit's states of a run, a worker and a plan", () => {
    expect(Object.keys(STATUS_LOOKS.run)).toEqual(
      expect.arrayContaining(["queued", "leased", "running", "verifying", "waiting", "parked", "review", "done", "failed", "lost", "cancelled"]),
    );
    expect(Object.keys(STATUS_LOOKS.worker).sort()).toEqual(["busy", "draining", "idle", "offline", "revoked"]);
    expect(Object.keys(STATUS_LOOKS.plan).sort()).toEqual(["active", "blocked", "completed", "pending"]);
  });

  it.each(CASES)("gives %s %s a Lucide icon and words in English and Vietnamese", (kind, status) => {
    const look = lookOf(kind, status);
    expect(look.icon).toBeTruthy();
    for (const locale of ["en", "vi"] as const) {
      const text = words(locale, kind, status);
      expect(text?.trim(), `${locale} status.${kind}.${status}`).toBeTruthy();
      const { unmount } = renderIn(locale, <StatusBadge kind={kind} status={status as never} />);
      const badge = screen.getByText(text).closest("[data-slot=status-badge]");
      expect(badge).not.toBeNull();
      expect(badge).toHaveAttribute("data-kind", kind);
      expect(badge).toHaveAttribute("data-status", status);
      expect(badge).toHaveAttribute("data-tone", look.tone);
      // The mark (icon or dot) is decoration: the word is the label, so colour is never the only cue.
      const mark = badge?.querySelector("[data-slot=status-mark]");
      expect(mark).not.toBeNull();
      expect(mark).toHaveAttribute("aria-hidden", "true");
      expect(badge).toHaveTextContent(text);
      unmount();
    }
  });

  it("pulses only for a running run, a busy worker and an active plan run", () => {
    const live = CASES.filter(([kind, status]) => lookOf(kind, status).dot === "live");
    expect(live).toEqual([
      ["run", "running"],
      ["worker", "busy"],
      ["plan", "active"],
    ]);
    renderIn("en", <StatusBadge kind="run" status="running" />);
    expect(screen.getByTestId("run-state").querySelector("[data-live=true] .animate-live-ping")).not.toBeNull();
  });

  it("shows other words for the same state when asked, with the default test id of its kind", () => {
    renderIn("en", <StatusBadge kind="run" status="waiting" label="Waiting for your decision" size="lg" />);
    const badge = screen.getByTestId("run-state");
    expect(badge).toHaveTextContent("Waiting for your decision");
    expect(badge).toHaveAttribute("data-status", "waiting");
  });

  it("keeps a status the hub does not know as written", () => {
    renderIn("vi", <OtherStatusBadge raw="skipped" />);
    expect(screen.getByText("Trạng thái khác: skipped").closest("[data-slot=status-badge]")).toHaveAttribute("data-tone", "outline");
  });
});
