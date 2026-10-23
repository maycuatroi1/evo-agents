"use client";

import { TrendingDown } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { ModeMark, modeRank, useAutonomySource } from "@/components/data/autonomy";
import { Badge } from "@/components/ui/badge";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

import { CLASSES, type Draft, figureCounts, modeAboveCeiling, presetMode } from "./model";
import type { AutonomySettings, SettingsClass } from "./queries";

const FIGURE_ORDER = ["refused", "asked", "decisions", "merged", "reverted", "rolled_back"] as const;

/** A class's last 30 days in one line; nothing for a class the hub records nothing of. */
function Figures({ settings, klass }: { settings: AutonomySettings; klass: string }) {
  const t = useTranslations("autonomy.figures");
  const counts = figureCounts(settings, klass);
  if (counts === null) return null;
  const parts = FIGURE_ORDER.filter((key) => counts[key] !== undefined).map((key) => t(key, { count: counts[key] ?? 0 }));
  return (
    <p className="text-xs text-muted-foreground" data-testid="autonomy-figures">
      {parts.length > 0 ? t("line", { days: settings.days, figures: parts.join(", ") }) : t("none", { days: settings.days })}
    </p>
  );
}

function ClassRow({ settings, klass, held, draft, onChange }: { settings: AutonomySettings; klass: string; held: SettingsClass; draft: Draft; onChange: (klass: string, mode: string) => void }) {
  const t = useTranslations("autonomy.settings");
  const tMode = useTranslations("autonomy.modes");
  const source = useAutonomySource(settings.principal);
  const id = useId();
  const value = draft.classes[klass] ?? "";
  const levelMode = presetMode(draft.level, klass);
  const conditioned = value.includes(":") ? value : null;
  const ceilingText = t("ceilingOf", { mode: tMode(held.ceiling), source: source(held.ceiling_source) });
  return (
    <li className="flex min-w-0 flex-col gap-2 px-4 py-3 sm:flex-row sm:items-start sm:gap-4" data-testid="autonomy-class" data-class={klass} data-lowered={held.lowered ? "true" : undefined}>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-mono text-xs font-semibold">{klass}</span>
          <span className="min-w-0 text-sm text-pretty [overflow-wrap:anywhere]">
            {held.name}
          </span>
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <ModeMark mode={held.mode} testId="autonomy-class-mode" />
          <span data-testid="autonomy-class-source">{t("setBy", { source: source(held.source) })}</span>
          {held.lowered && held.pref ? (
            <Badge variant="warning" title={held.detail} data-testid="autonomy-class-lowered">
              <TrendingDown aria-hidden="true" />
              {t("lowered", { mode: tMode(held.pref) })}
            </Badge>
          ) : null}
        </div>
        <Figures settings={settings} klass={klass} />
      </div>
      <div className="flex min-w-0 flex-col gap-1 sm:w-64 sm:shrink-0">
        <NativeSelect
          size="sm"
          className="w-full"
          value={value}
          onChange={(event) => onChange(klass, event.target.value)}
          aria-label={t("modeOf", { klass, name: held.name })}
          aria-describedby={modeRank(held.ceiling) < modeRank("auto") ? `${id}-ceiling` : undefined}
          data-testid="autonomy-class-select"
        >
          <NativeSelectOption value="">{t("levelDefault", { level: draft.level, mode: tMode(levelMode) })}</NativeSelectOption>
          {held.modes.map((mode) => {
            const above = modeAboveCeiling(settings, klass, mode);
            return (
              <NativeSelectOption key={mode} value={mode} disabled={above} data-above={above ? "true" : undefined}>
                {above ? t("modeAbove", { mode: tMode(mode), ceiling: tMode(held.ceiling), source: source(held.ceiling_source) }) : tMode(mode)}
              </NativeSelectOption>
            );
          })}
          {conditioned ? <NativeSelectOption value={conditioned}>{conditioned}</NativeSelectOption> : null}
        </NativeSelect>
        {modeRank(held.ceiling) < modeRank("auto") ? (
          <p id={`${id}-ceiling`} className="text-xs text-muted-foreground" data-testid="autonomy-class-ceiling">
            {ceilingText}
          </p>
        ) : null}
      </div>
    </li>
  );
}

/** The twelve classes: each one's effective mode and source, a flag when the ceiling lowers it below the preference,
 * its figures, and the override the preference sets for it. */
export function ClassList({ settings, draft, onChange }: { settings: AutonomySettings; draft: Draft; onChange: (klass: string, mode: string) => void }) {
  const t = useTranslations("autonomy.settings");
  return (
    <section aria-labelledby="autonomy-classes-title" className="flex min-w-0 flex-col gap-2">
      <h2 id="autonomy-classes-title" className="text-sm font-semibold">
        {t("classesTitle")}
      </h2>
      <ul className="flex min-w-0 flex-col divide-y divide-border rounded-md border bg-card" data-testid="autonomy-classes">
        {CLASSES.map((klass) => {
          const held = settings.classes[klass];
          return held ? <ClassRow key={klass} settings={settings} klass={klass} held={held} draft={draft} onChange={onChange} /> : null;
        })}
      </ul>
    </section>
  );
}
