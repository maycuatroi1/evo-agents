"use client";

import { TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

import { TERMS_WARNING } from "./model";

/** A field of the share dialog: its label, the control, a hint and, once the form was sent, its problem. */
export function ShareField({
  id,
  label,
  hint,
  problem,
  children,
  className,
  testId,
}: {
  id: string;
  testId: string;
  label: string;
  hint?: string;
  problem: string | null;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-pretty text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {problem ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger" data-testid={`${testId}-error`}>
          {problem}
        </p>
      ) : null}
    </div>
  );
}

/** The ids a control names in aria-describedby: its hint, and its problem when it has one. */
export const describedBy = (id: string, problem: string | null, hint = true) =>
  [hint ? `${id}-hint` : null, problem ? `${id}-error` : null].filter(Boolean).join(" ") || undefined;

/**
 * The warning shown before a personal subscription token is shared: the hub's own text, word for word and in English
 * in every language, since the hub keeps it so in the audit, then the box its owner ticks to share it anyway. Share
 * stays off until it is ticked.
 */
export function TermsWarning({ id, checked, onChange }: { id: string; checked: boolean; onChange: (checked: boolean) => void }) {
  const t = useTranslations("sharing.terms");
  return (
    <section
      aria-labelledby={`${id}-title`}
      className="flex flex-col gap-3 rounded-md border border-attention/30 bg-attention-soft px-3 py-3 text-sm"
      data-testid="share-terms"
    >
      <div className="flex items-start gap-2.5 text-attention">
        <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <div className="flex min-w-0 flex-col gap-1">
          <h3 id={`${id}-title`} className="font-medium">
            {t("title")}
          </h3>
          <p lang="en" className="text-pretty text-foreground" data-testid="share-terms-text">
            {TERMS_WARNING}
          </p>
        </div>
      </div>
      <label
        htmlFor={id}
        className="flex min-h-10 cursor-pointer items-start gap-2.5 rounded-md border border-attention/30 bg-card px-3 py-2 text-foreground"
      >
        <input
          id={id}
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
          className="mt-0.5 size-4 shrink-0 cursor-pointer accent-primary"
          data-testid="share-terms-ack"
        />
        <span className="text-pretty">{t("ack")}</span>
      </label>
    </section>
  );
}
