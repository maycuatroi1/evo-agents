"use client";

import { Eye, type LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type Tone = "neutral" | "review" | "attention" | "success" | "danger";

const TONES: Record<Tone, string> = {
  neutral: "bg-surface-sunken text-muted-foreground",
  review: "border-review/25 bg-review-soft text-review",
  attention: "border-attention/25 bg-attention-soft text-attention",
  success: "border-success/25 bg-success-soft text-success",
  danger: "border-danger/30 bg-danger-soft text-danger",
};

/**
 * A banner across a page or a card: the tone's icon, a title in the text colour of its tone and one or two lines under
 * it, and its actions on the right, which wrap under the text on a phone. A `note` by default; an error that stops a
 * write is an `alert`, so a screen reader says it as it appears.
 */
export function Banner({
  icon: Icon,
  title,
  children,
  actions,
  tone = "neutral",
  role = "note",
  className,
  testId,
}: {
  icon: LucideIcon;
  title: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  tone?: Tone;
  role?: "note" | "alert" | "status";
  className?: string;
  testId?: string;
}) {
  return (
    <div
      role={role}
      className={cn("flex flex-wrap items-start gap-x-3 gap-y-2 rounded-md border px-4 py-3 text-[13px] leading-[18px]", TONES[tone], className)}
      data-testid={testId}
    >
      <Icon className="mt-px size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-1 basis-56 flex-col gap-1">
        <p className={cn("font-medium text-pretty [overflow-wrap:anywhere]", tone === "neutral" && "text-foreground")}>{title}</p>
        {children ? <div className="text-pretty text-muted-foreground [overflow-wrap:anywhere]">{children}</div> : null}
      </div>
      {actions ? <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

/** What a reader of the project may do on this page: read, never write. */
export function ReaderNote({ project, children }: { project: string; children: ReactNode }) {
  const t = useTranslations("srs");
  return (
    <Banner icon={Eye} title={t("readerTitle", { project })} testId="srs-read-only">
      {children}
    </Banner>
  );
}
