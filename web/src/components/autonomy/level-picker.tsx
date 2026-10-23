"use client";

import { Lock } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { useAutonomySource } from "@/components/data/autonomy";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

import { LEVELS, levelAboveCeiling } from "./model";
import type { AutonomySettings, Level } from "./queries";

function isLevel(value: string): value is Level {
  return (LEVELS as readonly string[]).includes(value);
}

/**
 * The six levels as one radio group, each with what it lets agents do. A level above the ceiling stays in the list,
 * dimmed and out of reach, with the ceiling and the source that sets it. Native radios, so the arrow keys move and
 * check at once and skip a disabled level, as the browser does it.
 */
export function LevelPicker({ settings, value, onChange }: { settings: AutonomySettings; value: Level; onChange: (level: Level) => void }) {
  const t = useTranslations("autonomy.settings");
  const tLevel = useTranslations("autonomy.levels");
  const source = useAutonomySource(settings.principal);
  const ids = useId();
  const name = `${ids}-level`;
  const by = settings.ceiling_detail
    ? t("sourceDetail", {
        source: source(settings.ceiling_source),
        detail: settings.ceiling_detail,
      })
    : source(settings.ceiling_source);
  const reason = t("aboveCeiling", { ceiling: settings.ceiling, source: by });

  return (
    <fieldset className="flex min-w-0 flex-col gap-2">
      <legend className="mb-1.5 text-sm font-semibold">{t("levelTitle")}</legend>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3" data-testid="autonomy-level-picker">
        {LEVELS.map((level) => {
          const above = levelAboveCeiling(settings, level);
          const id = `${ids}-${level}`;
          return (
            <Label
              key={level}
              htmlFor={id}
              className={cn(
                "flex min-w-0 items-start gap-3 rounded-md border px-3 py-2.5 font-normal transition-colors has-checked:border-brand/40 has-checked:bg-surface-selected has-focus-visible:ring-3 has-focus-visible:ring-ring/50",
                above ? "cursor-not-allowed opacity-60" : "cursor-pointer hover:bg-muted/50",
              )}
              data-testid="autonomy-level-option"
              data-level={level}
              data-above={above ? "true" : undefined}
            >
              <input
                id={id}
                type="radio"
                name={name}
                value={level}
                checked={value === level}
                disabled={above}
                onChange={(event) => {
                  if (isLevel(event.target.value)) onChange(event.target.value);
                }}
                className="mt-0.5 size-4 shrink-0 accent-primary outline-none disabled:cursor-not-allowed"
                aria-labelledby={`${id}-name`}
                aria-describedby={`${id}-hint${above ? ` ${id}-why` : ""}`}
              />
              <span className="flex min-w-0 flex-col gap-1">
                <span id={`${id}-name`} className="text-sm font-medium">
                  {tLevel(`${level}.name`)}
                </span>
                <span id={`${id}-hint`} className="text-xs leading-snug text-pretty text-muted-foreground">
                  {tLevel(`${level}.hint`)}
                </span>
                {above ? (
                  <span
                    id={`${id}-why`}
                    className="inline-flex items-start gap-1 text-xs leading-snug text-pretty text-muted-foreground"
                    data-testid="autonomy-level-why"
                  >
                    <Lock className="mt-0.5 size-3 shrink-0" aria-hidden="true" />
                    {reason}
                  </span>
                ) : null}
              </span>
            </Label>
          );
        })}
      </div>
    </fieldset>
  );
}
