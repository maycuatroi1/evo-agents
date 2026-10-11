"use client";

import { Layers } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

import { Section } from "./dispatch-fields";
import { runsSessionPerStep } from "./model";

/** A worker as the field reads it: what its daemon says it runs (the dialog hands it the visitor's workers). */
type SessionsWorker = Parameters<typeof runsSessionPerStep>[0];

/**
 * The switch between one session for every step and a new session for each step, with what that means and how many
 * of the visitor's workers for the project run it (a worker whose daemon is older runs only one session): a field of
 * the Run plan dialog, which loads it when it opens (plan-run-dialog.tsx).
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
  return (
    <Section legend={t("legend")} testId="plan-run-sessions">
      <div className={cn("flex min-h-11 items-center gap-3 rounded-md border px-3 py-2 transition-colors", on && "border-brand/40 bg-surface-selected")}>
        <Layers className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <Label htmlFor={`${ids}-switch`} className="min-w-0 flex-1 cursor-pointer font-normal">
          {t("label")}
        </Label>
        <Switch
          id={`${ids}-switch`}
          checked={on}
          onCheckedChange={onChange}
          disabled={disabled}
          aria-describedby={`${ids}-hint`}
          data-testid="plan-run-session-per-step"
        />
      </div>
      <p id={`${ids}-hint`} className="text-xs text-pretty text-muted-foreground">
        {on ? t("hintOn") : t("hintOff")}
        {on && loaded && workers.length > 0 ? (
          <span className="block" data-testid="plan-run-sessions-reach" data-count={count}>
            {count === 0 ? t("reachNone") : t("reach", { count, total: workers.length })}
          </span>
        ) : null}
      </p>
    </Section>
  );
}
