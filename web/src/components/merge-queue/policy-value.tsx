"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useCallback } from "react";

import { messageKey, splitKey } from "./policy-model";

type Words = ReturnType<typeof useTranslations<"policy">>;

/** The words of a key: "Hold the next merge until", "UAT check job". */
export function useKeyLabel() {
  const t = useTranslations("policy.keys");
  return useCallback((path: string) => t(`${messageKey(path)}.label` as Parameters<typeof t>[0]), [t]);
}

/** A dotted key of a comparison as people read it: "shop: PROD release", "Hold the next merge until". */
export function useDottedLabel() {
  const label = useKeyLabel();
  return useCallback(
    (key: string) => {
      const { repo, path } = splitKey(key);
      return repo === null ? label(path) : `${repo}: ${label(path)}`;
    },
    [label],
  );
}

function choice(t: Words, path: string, value: string): string {
  const key = `values.${messageKey(path)}.${value}`;
  return t.has(key as Parameters<Words>[0]) ? t(key as Parameters<Words>[0]) : value;
}

/**
 * A key's value as people read it: a choice in words, a rate as a percentage, minutes and hours with their unit, a
 * null in what it means ("the repo's default branch", "none"), a list of globs joined.
 */
export function useValueText() {
  const t = useTranslations("policy");
  const format = useFormatter();
  return useCallback(
    (key: string, value: unknown): string => {
      const { path } = splitKey(key);
      if (value === null || value === undefined) {
        const none = `none.${messageKey(path)}`;
        return t.has(none as Parameters<Words>[0]) ? t(none as Parameters<Words>[0]) : t("none.default");
      }
      if (Array.isArray(value)) return value.length === 0 ? t("none.list") : value.join(", ");
      if (typeof value === "string") return choice(t, path, value);
      if (typeof value === "number") {
        if (path === "breaker.revert_rate_max" || path === "audit.sample_rate") return format.number(value, { style: "percent", maximumFractionDigits: 2 });
        if (path === "breaker.revert_rate_factor") return t("units.times", { value: format.number(value) });
        if (path.endsWith("_minutes")) return t("units.minutes", { value });
        if (path.endsWith("_hours")) return t("units.hours", { value: format.number(value) });
        return format.number(value);
      }
      if (typeof value === "boolean") return String(value);
      return JSON.stringify(value);
    },
    [t, format],
  );
}
