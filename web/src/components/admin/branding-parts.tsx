"use client";

import { CircleCheck, CircleX, Network, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { BrandToken, Theme } from "@/lib/hub-settings";
import { cn } from "@/lib/utils";

import { type CheckResult, DEFAULTS, HEX, INK, shownRatio } from "./branding-model";

/**
 * One token of one theme in the Branding form: a colour picker and the same colour as #rrggbb text, both naming the
 * token and the theme, the default under it, and a reset to that default. Empty text leaves the token as
 * web/DESIGN.md has it.
 */
export function ColorField({
  id,
  theme,
  token,
  value,
  error,
  onChange,
}: {
  id: string;
  theme: Theme;
  token: BrandToken;
  value: string;
  error?: string;
  onChange: (value: string) => void;
}) {
  const t = useTranslations("admin.branding");
  const fallback = DEFAULTS[theme][token];
  const shown = HEX.test(value) ? value.toLowerCase() : fallback;
  const name = t(`tokens.${token}`);
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid={`color-${theme}-${token}`}>
      <Label htmlFor={id}>{name}</Label>
      <div className="flex items-center gap-2">
        <input
          type="color"
          value={shown}
          onChange={(event) => onChange(event.target.value)}
          aria-label={t("picker", { token: name, theme: t(`themes.${theme}`) })}
          className="h-9 w-11 shrink-0 cursor-pointer rounded-sm border border-input bg-card p-0.5 max-md:h-11"
          data-testid={`color-${theme}-${token}-picker`}
        />
        <Input
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value.trim())}
          placeholder={fallback}
          maxLength={7}
          autoComplete="off"
          spellCheck={false}
          className="h-9 min-w-0 font-mono"
          aria-invalid={error ? true : undefined}
          aria-describedby={`${id}-hint${error ? ` ${id}-error` : ""}`}
          data-testid={`color-${theme}-${token}-input`}
        />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t("reset", { token: name, theme: t(`themes.${theme}`) })}
          aria-disabled={value === "" || undefined}
          onClick={() => onChange("")}
        >
          <Undo2 aria-hidden="true" />
        </Button>
      </div>
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        {t("defaultColor", { color: fallback })}
      </p>
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** The pairs of one theme with their ratio and whether each reaches what web/DESIGN.md asks, told by icon and word. */
export function ThemeChecks({ theme, results }: { theme: Theme; results: CheckResult[] }) {
  const t = useTranslations("admin.branding");
  return (
    <ul aria-label={t("checksLabel", { theme: t(`themes.${theme}`) })} className="flex flex-col gap-1.5" data-testid={`checks-${theme}`}>
      {results.map((result) => (
        <li
          key={result.id}
          className="flex items-start gap-2 text-[13px] leading-[18px]"
          data-testid={`check-${theme}-${result.id}`}
          data-passes={result.passes}
        >
          {result.passes ? (
            <CircleCheck className="mt-px size-4 shrink-0 text-success" aria-hidden="true" />
          ) : (
            <CircleX className="mt-px size-4 shrink-0 text-danger" aria-hidden="true" />
          )}
          <span className="min-w-0 flex-1">
            <span className="text-foreground">{t(`checks.${result.id}`)}</span>{" "}
            <span className="text-muted-foreground">
              {t("pair", { front: t(`tokens.${result.front}`), ground: t(`grounds.${result.ground}`) })}
            </span>
          </span>
          <span className={cn("shrink-0 tabular-nums", result.passes ? "text-muted-foreground" : "font-medium text-danger")}>
            {t(result.passes ? "passes" : "fails", { ratio: shownRatio(result.ratio), least: result.least })}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * How the tokens of one theme draw the parts that use them: the logo tile, the selected nav item, a link and a link
 * under the pointer, the running pill. The colours are the ones typed (the defaults where none is), painted inline
 * since they are not the page's own tokens yet.
 */
export function ThemePreview({ theme, palette, name }: { theme: Theme; palette: Record<string, string>; name: string }) {
  const t = useTranslations("admin.branding.preview");
  const ink = INK[theme];
  return (
    <div
      role="img"
      aria-label={t("label", { theme: theme === "light" ? t("light") : t("dark") })}
      className="flex flex-col gap-2.5 rounded-md border p-3"
      style={{ backgroundColor: palette.canvas, color: ink }}
      data-testid={`preview-${theme}`}
    >
      <div className="flex items-center gap-2 rounded-sm p-1.5" style={{ backgroundColor: palette.surface }}>
        <span
          className="inline-flex size-6 items-center justify-center rounded-sm text-xs font-semibold"
          style={{ backgroundColor: palette.brand, color: palette["on-brand"] }}
        >
          {name.slice(0, 1).toUpperCase() || "E"}
        </span>
        <span className="truncate text-sm font-semibold">{name}</span>
      </div>
      <div className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-[13px]" style={{ backgroundColor: palette["surface-selected"] }}>
        <Network className="size-4" style={{ color: palette.brand }} aria-hidden="true" />
        {t("navItem")}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-sm p-2 text-[13px]" style={{ backgroundColor: palette.surface }}>
        <span className="underline underline-offset-4" style={{ color: palette.brand }}>
          {t("link")}
        </span>
        <span className="underline underline-offset-4" style={{ color: palette["brand-hover"] }}>
          {t("hover")}
        </span>
        <span
          className="inline-flex h-5 items-center gap-1.5 rounded-full px-2 text-xs font-medium"
          style={{ backgroundColor: palette["brand-soft"], color: palette.brand }}
        >
          <span className="size-1.5 rounded-full" style={{ backgroundColor: palette.brand }} />
          {t("running")}
        </span>
      </div>
    </div>
  );
}
