"use client";

import type { ReactNode } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** One of the page's four cards: its number and name as its heading, the anchor the step strip links to. */
export function StepCard({ id, title, note, testId, children }: { id: string; title: string; note?: ReactNode; testId: string; children: ReactNode }) {
  return (
    <Card id={id} className="scroll-mt-20" aria-labelledby={`${id}-title`} role="region" data-testid={testId}>
      <CardHeader>
        <CardTitle>
          <h2 id={`${id}-title`}>{title}</h2>
        </CardTitle>
        {note ? <p className="text-xs text-fg-subtle">{note}</p> : null}
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">{children}</CardContent>
    </Card>
  );
}
