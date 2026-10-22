"use client";

import { Ban, Bell, CircleCheck, MessageCircleQuestionMark, type LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

/**
 * The words and marks of autonomy (plan autonomy-levels) that the settings page, a run's card and the org chart share:
 * the four modes of a class, strictest first, and where a mode or level comes from, as the hub names its sources
 * (`pref:<login>`, `seat:<id>`, `unit:<slug>`, `policy:<project>/<repo>`, `profile:<handle>`, `credential_share`,
 * `instance`). A mode is a quiet mark, never a warning: the pages flag what is unusual, not the mode itself.
 */
export const MODES = ["deny", "ask", "notify", "auto"] as const;
export type Mode = (typeof MODES)[number];
export const AUTONOMY_LEVELS = ["A0", "A1", "A2", "A3", "A4", "A5"] as const;

export function modeRank(mode: string): number {
  return MODES.indexOf(mode as Mode);
}

export function levelRank(level: string): number {
  return AUTONOMY_LEVELS.indexOf(level as (typeof AUTONOMY_LEVELS)[number]);
}

const ICONS: Record<Mode, LucideIcon> = { deny: Ban, ask: MessageCircleQuestionMark, notify: Bell, auto: CircleCheck };

/** One mode as a small mark with its icon and word; the icon tells the modes apart without colour. */
export function ModeMark({ mode, className, testId }: { mode: string; className?: string; testId?: string }) {
  const t = useTranslations("autonomy.modes");
  const known = (MODES as readonly string[]).includes(mode) ? (mode as Mode) : null;
  const Icon = known ? ICONS[known] : null;
  return (
    <span
      className={cn(
        "inline-flex h-5 w-fit shrink-0 items-center gap-1 rounded-sm border px-1.5 text-xs font-medium whitespace-nowrap",
        known === "auto" ? "text-muted-foreground" : "text-foreground",
        className,
      )}
      data-mode={mode}
      data-testid={testId}
    >
      {Icon ? <Icon className="size-3 shrink-0" aria-hidden="true" /> : null}
      {known ? t(known) : mode}
    </span>
  );
}

/** Where a mode or a level comes from, in words; `self` is the viewer's login, whose preference reads "your preference". */
export function useAutonomySource(self?: string | null) {
  const t = useTranslations("autonomy.sources");
  return (source: string): string => {
    const at = source.indexOf(":");
    const kind = at < 0 ? source : source.slice(0, at);
    const name = at < 0 ? "" : source.slice(at + 1);
    switch (kind) {
      case "pref":
        return name === self ? t("ownPref") : t("pref", { login: name });
      case "seat":
        return t("seat", { id: name });
      case "unit":
        return t("unit", { slug: name });
      case "policy":
        return t("policy", { repo: name });
      case "profile":
        return t("profile", { handle: name });
      case "credential_share":
        return t("credential");
      case "instance":
        return t("instance");
      default:
        return source;
    }
  };
}
