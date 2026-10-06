"use client";

import { FileQuestion, type LucideIcon, ServerCrash, ShieldX, TriangleAlert, WifiOff } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { type ApiErrorInfo, errorKind } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

/**
 * The loading, empty and error states every data view uses. Each state is a landmark-free block with a heading,
 * so it reads the same to a screen reader wherever it appears; colour is never the only signal (icon and text).
 */

type StateProps = {
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  tone?: "neutral" | "danger" | "warning";
  children?: ReactNode;
  className?: string;
  testId?: string;
};

const TONES = {
  neutral: "bg-muted text-muted-foreground",
  danger: "bg-danger-soft text-danger",
  warning: "bg-attention-soft text-attention",
} as const;

export function StatePanel({ icon: Icon, title, description, tone = "neutral", children, className, testId }: StateProps) {
  return (
    <section
      data-testid={testId}
      className={cn(
        "mx-auto flex w-full max-w-xl flex-col items-center gap-4 rounded-md border border-dashed bg-card px-6 py-10 text-center",
        className,
      )}
    >
      <span className={cn("flex size-12 items-center justify-center rounded-full", TONES[tone])} aria-hidden="true">
        <Icon className="size-6" />
      </span>
      <div className="flex flex-col gap-1.5">
        <h2 className="text-lg font-semibold tracking-tight text-balance">{title}</h2>
        {description ? <div className="text-sm text-pretty text-muted-foreground">{description}</div> : null}
      </div>
      {children ? <div className="flex flex-wrap items-center justify-center gap-2">{children}</div> : null}
    </section>
  );
}

export function EmptyState(props: Omit<StateProps, "tone">) {
  return <StatePanel tone="neutral" testId="state-empty" {...props} />;
}

function BackHome() {
  const t = useTranslations("states");
  return (
    <Button asChild variant="outline" size="lg">
      <Link href="/">{t("backHome")}</Link>
    </Button>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <span>{label}:</span>
      <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground break-all">{children}</code>
    </div>
  );
}

export function ForbiddenState({ error }: { error?: ApiErrorInfo }) {
  const t = useTranslations("states");
  return (
    <StatePanel
      icon={ShieldX}
      tone="warning"
      title={t("forbiddenTitle")}
      description={t("forbiddenDescription")}
      testId="state-forbidden"
    >
      <BackHome />
      {error?.requestId ? (
        <div className="basis-full">
          <Detail label={t("requestId")}>{error.requestId}</Detail>
        </div>
      ) : null}
    </StatePanel>
  );
}

export function NotFoundState({ title, description }: { title?: string; description?: string }) {
  const t = useTranslations("states");
  return (
    <StatePanel
      icon={FileQuestion}
      title={title ?? t("notFoundTitle")}
      description={description ?? t("notFoundDescription")}
      testId="state-not-found"
    >
      <BackHome />
    </StatePanel>
  );
}

/** A 5xx, no answer, or an unexpected 4xx: what failed, the request id to report, and a retry. */
export function ServerErrorState({ error, onRetry }: { error: ApiErrorInfo; onRetry?: () => void }) {
  const t = useTranslations("states");
  const kind = errorKind(error.status);
  const [icon, title, description] =
    kind === "network"
      ? [WifiOff, t("networkErrorTitle"), t("networkErrorDescription")]
      : kind === "server"
        ? [ServerCrash, t("serverErrorTitle"), t("serverErrorDescription")]
        : [TriangleAlert, t("clientErrorTitle"), error.message];
  return (
    <StatePanel icon={icon} tone="danger" title={title} description={description} testId="state-error">
      {onRetry ? (
        <Button size="lg" onClick={onRetry}>
          {t("retry")}
        </Button>
      ) : null}
      <BackHome />
      <div className="flex basis-full flex-col gap-1.5 pt-2">
        {error.requestId ? <Detail label={t("requestId")}>{error.requestId}</Detail> : null}
        {error.status ? <Detail label={t("status", { status: error.status })}>{error.code}</Detail> : null}
      </div>
    </StatePanel>
  );
}

/** The state for any failed API call except 401, which never reaches a page (it goes to the sign-in page). */
export function ApiErrorState({ error, onRetry }: { error: ApiErrorInfo; onRetry?: () => void }) {
  const kind = errorKind(error.status);
  if (kind === "forbidden") return <ForbiddenState error={error} />;
  if (kind === "not_found") return <NotFoundState />;
  return <ServerErrorState error={error} onRetry={onRetry} />;
}

/** Announced once to screen readers; the skeleton itself is decoration. */
export function LoadingState({ children, className }: { children?: ReactNode; className?: string }) {
  const t = useTranslations("states");
  return (
    <div role="status" aria-live="polite" aria-busy="true" className={className} data-testid="state-loading">
      <span className="sr-only">{t("loading")}</span>
      <div aria-hidden="true">{children ?? <PageSkeleton />}</div>
    </div>
  );
}

export function PageSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-7 w-56" />
        <Skeleton className="h-4 w-full max-w-md" />
      </div>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {Array.from({ length: 3 }, (_, i) => (
          <Skeleton key={i} className="h-32 w-full rounded-md" />
        ))}
      </div>
      <TableSkeleton rows={5} />
    </div>
  );
}

export function TableSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-2 rounded-md border bg-card shadow-raised p-4">
      <Skeleton className="h-5 w-40" />
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  );
}
