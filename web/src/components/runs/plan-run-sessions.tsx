"use client";

import { Layers } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { InfoTip } from "@/components/feedback/info-tip";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

import { runsSessionPerStep } from "./model";

/** A worker as the field reads it: what its daemon says it runs (the dialog hands it the visitor's workers). */
type SessionsWorker = Parameters<typeof runsSessionPerStep>[0];

/**
 * The switch between one session for every step and a new session for each step: a field of the Run plan dialog,
 * which loads it when it opens (plan-run-dialog.tsx). What each means shows from the info icon; below the switch, only
 * that none of the visitor's workers for the project runs a session for each step yet (an older daemon runs one).
 */
export function SessionsField({
  on,
  onChange,
  disabled,
  workers,
  loaded,
}: {
  on: boolean;
  onChange: (on: boolean) => void;
  disabled: boolean;
  workers: readonly SessionsWorker[];
  loaded: boolean;
}) {
  const t = useTranslations("runs.planRun.sessions");
  const ids = useId();
  const count = workers.filter(runsSessionPerStep).length;
  const none = on && loaded && workers.length > 0 && count === 0;
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="plan-run-sessions">
      <div className="flex items-center gap-1.5">
        <span className="text-sm font-medium" id={`${ids}-legend`}>
          {t("legend")}
        </span>
        <InfoTip label={t("legend")} testId="plan-run-sessions-info">
          <span>{t("hintOff")}</span>
          <span>{t("hintOn")}</span>
        </InfoTip>
      </div>
      <div className={cn("flex min-h-9 items-center gap-3 rounded-md border px-3 py-1.5 transition-colors max-md:min-h-11", on && "border-brand/40 bg-surface-selected")}>
        <Layers className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <Label htmlFor={`${ids}-switch`} className="min-w-0 flex-1 cursor-pointer font-normal">
          {t("label")}
        </Label>
        <Switch
          id={`${ids}-switch`}
          checked={on}
          onCheckedChange={onChange}
          disabled={disabled}
          aria-describedby={none ? `${ids}-none` : undefined}
          data-testid="plan-run-session-per-step"
        />
      </div>
      {none ? (
        <p id={`${ids}-none`} className="text-xs text-pretty text-attention" data-testid="plan-run-sessions-reach" data-count={count}>
          {t("reachNone")}
        </p>
      ) : null}
    </div>
  );
}
