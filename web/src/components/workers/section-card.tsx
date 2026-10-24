import type { ReactNode } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * One card of a worker's page: a heading, with the number of what it lists when `count` is given, one sentence on where
 * its facts come from, and the facts.
 */
export function SectionCard({
  title,
  description,
  children,
  testId,
  count,
  className,
}: {
  title: string;
  description: ReactNode;
  children: ReactNode;
  testId: string;
  count?: number;
  className?: string;
}) {
  return (
    <Card className={cn("min-w-0", className)} data-testid={testId}>
      <CardHeader>
        <CardTitle>
          <h2>
            {title}
            {count !== undefined ? <span className="ml-2 font-normal text-muted-foreground tabular-nums">{count}</span> : null}
          </h2>
        </CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0">{children}</CardContent>
    </Card>
  );
}
