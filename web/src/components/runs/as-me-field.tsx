"use client";

import { TriangleAlert, UserRoundKey } from "lucide-react";
import { useTranslations } from "next-intl";

import { Choice } from "./dispatch-fields";

/**
 * "Run as me on the hub" in Dispatch and Run plan (docs/credentials.md, "A run that runs as its owner"): off by
 * default, a checkbox card that says what the run's agent gets, the owner's access on every project, and what it
 * never gets, their secrets, tokens and workers, and that it ends with the run. Once on, a warning says the risk:
 * what the agent reads may steer it with that access, and where to revoke it. The form sends `as_me` either way.
 */
export function AsMeField({ checked, onChange, disabled = false }: { checked: boolean; onChange: (checked: boolean) => void; disabled?: boolean }) {
  const t = useTranslations("runs.asMe");
  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid="dispatch-as-me">
      <Choice
        type="checkbox"
        name="as_me"
        value="as_me"
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        title={
          <span className="inline-flex items-center gap-1.5 font-medium">
            <UserRoundKey className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            {t("label")}
          </span>
        }
        hint={t("hint")}
        testId="dispatch-as-me-choice"
      />
      {checked ? (
        <p
          className="flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-[13px] leading-[19px] text-attention"
          role="note"
          data-testid="dispatch-as-me-warning"
        >
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span className="text-pretty">{t("warning")}</span>
        </p>
      ) : null}
    </div>
  );
}
