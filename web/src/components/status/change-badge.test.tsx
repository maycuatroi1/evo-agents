// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import en from "../../../messages/en.json";
import vi from "../../../messages/vi.json";

import { CHANGE_LOOKS, ChangeBadge, type ChangeStatus } from "./change-badge";
import { STATUS_LOOKS } from "./status-badge";

const MESSAGES = { en, vi } as const;
type Locale = keyof typeof MESSAGES;

function renderIn(locale: Locale, ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale={locale} messages={MESSAGES[locale]} timeZone="Asia/Ho_Chi_Minh">
      {ui}
    </NextIntlClientProvider>,
  );
}

const STATES = Object.keys(CHANGE_LOOKS) as ChangeStatus[];

describe("ChangeBadge", () => {
  it("covers every state of a change, and the shared map leaves the kind to this file", () => {
    expect(STATES.sort()).toEqual(["closed", "judge_pending", "judged", "judging", "merged", "open", "planned", "pr_pending"]);
    expect(Object.keys(STATUS_LOOKS)).not.toContain("change");
  });

  it.each(STATES)("gives change %s a Lucide icon and words in English and Vietnamese", (status) => {
    const look = CHANGE_LOOKS[status];
    expect(look.icon).toBeTruthy();
    for (const locale of ["en", "vi"] as const) {
      const text = (MESSAGES[locale].status.change as Record<string, string>)[status];
      expect(text?.trim(), `${locale} status.change.${status}`).toBeTruthy();
      const { unmount } = renderIn(locale, <ChangeBadge status={status} />);
      const badge = screen.getByTestId("change-state");
      expect(badge).toHaveAttribute("data-slot", "status-badge");
      expect(badge).toHaveAttribute("data-kind", "change");
      expect(badge).toHaveAttribute("data-status", status);
      expect(badge).toHaveAttribute("data-tone", look.tone);
      expect(badge.querySelector("[data-slot=status-mark]")).toHaveAttribute("aria-hidden", "true");
      expect(badge).toHaveTextContent(text);
      unmount();
    }
  });

  it("leaves a change open for its owner in attention", () => {
    renderIn("en", <ChangeBadge status="open" />);
    expect(screen.getByTestId("change-state")).toHaveAttribute("data-tone", "attention");
  });
});
