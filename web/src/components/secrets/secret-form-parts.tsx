"use client";

import type { Info } from "lucide-react";
import type { ReactNode } from "react";

/** A field's problem, tied to it by id for screen readers. */
export function FieldError({ id, text, testId }: { id: string; text?: string | null; testId: string }) {
  if (!text) return null;
  return (
    <p id={id} className="text-xs font-medium text-danger" data-testid={testId}>
      {text}
    </p>
  );
}

/** A field's hint, tied to it by id. */
export function Hint({ id, children }: { id: string; children: ReactNode }) {
  return (
    <p id={id} className="text-xs text-pretty text-muted-foreground">
      {children}
    </p>
  );
}

/** A note of the form: a warning in `attention`, or a neutral one. */
export function Box({ tone, icon: Icon, children, testId }: { tone: "info" | "warning"; icon: typeof Info; children: ReactNode; testId?: string }) {
  return (
    <p
      className={
        tone === "warning"
          ? "flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-sm text-attention"
          : "flex items-start gap-2.5 rounded-md border bg-muted px-3 py-2.5 text-sm text-foreground"
      }
      data-testid={testId}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span className="text-pretty">{children}</span>
    </p>
  );
}
