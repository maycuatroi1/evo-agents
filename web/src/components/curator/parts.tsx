import { useTranslations } from "next-intl";

import type { ShiftHours } from "./shift-hours";

/** A link inside a line of text: `brand` with a quiet underline, so colour is not all that sets it apart (axe). */
export const TEXT_LINK = "rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current";

/**
 * Small pieces the Curator's pages share: a list of facts (term over value on a phone, side by side from the sm
 * breakpoint) and money as the hub counts it, in US dollars. They live in components/data/facts.tsx, which the
 * Releases page shares too.
 */
export { Fact, Facts, useDay, useMoney } from "@/components/data/facts";

/**
 * When the Curator works, as a line: its window, or the period it is paced over and its quiet hours. `empty` says
 * when its first run comes, for a list of nights still empty.
 */
export function useShiftText(): { hours: (hours: ShiftHours) => string; empty: (hours: ShiftHours) => string } {
  const t = useTranslations("curator.schedule");
  const tNights = useTranslations("curator.nights");
  const tPeriod = useTranslations("curator.periods");
  return {
    hours: (hours) =>
      hours.kind === "window"
        ? t("windowValue", { start: hours.start, end: hours.end, timezone: hours.timezone })
        : hours.quiet
          ? t("pacedValue", { period: tPeriod(hours.period), start: hours.quiet.start, end: hours.quiet.end, timezone: hours.timezone })
          : t("pacedAlways", { period: tPeriod(hours.period), timezone: hours.timezone }),
    empty: (hours) =>
      hours.kind === "window"
        ? tNights("empty", { start: hours.start, timezone: hours.timezone })
        : tNights("emptyPaced", { period: tPeriod(hours.period), timezone: hours.timezone }),
  };
}
