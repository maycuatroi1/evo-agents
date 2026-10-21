"use client";

import { Eye } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Banner } from "@/components/feedback/banner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** What a reader of the project may do on these pages: read, never write. */
export function ReaderNote({ project, children }: { project: string; children: ReactNode }) {
  const t = useTranslations("canvas");
  return (
    <Banner icon={Eye} title={t("readerTitle", { project })} testId="canvas-read-only">
      {children}
    </Banner>
  );
}

/** A section of a page as a card: its h2, an optional count or action on the right, then its content. */
export function SectionCard({
  title,
  action,
  children,
  testId,
  headingId,
}: {
  title: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  testId?: string;
  headingId?: string;
}) {
  return (
    <Card size="sm" data-testid={testId} aria-labelledby={headingId} role={headingId ? "region" : undefined}>
      <CardHeader className="flex flex-wrap items-center justify-between gap-2">
        <CardTitle>
          <h2 id={headingId}>{title}</h2>
        </CardTitle>
        {action}
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-3">{children}</CardContent>
    </Card>
  );
}

/** A term and its value in a list of facts, the value in the text colour. */
export function Fact({ term, children, testId }: { term: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5" data-testid={testId}>
      <dt className="text-xs text-muted-foreground">{term}</dt>
      <dd className="min-w-0 text-[13px] text-foreground [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}
