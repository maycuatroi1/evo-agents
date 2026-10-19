import type { ReactNode } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/** One card of a worker's page: a heading, one sentence on where its facts come from, and the facts. */
export function SectionCard({ title, description, children, testId }: { title: string; description: string; children: ReactNode; testId: string }) {
  return (
    <Card className="min-w-0" data-testid={testId}>
      <CardHeader>
        <CardTitle>
          <h2>{title}</h2>
        </CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0">{children}</CardContent>
    </Card>
  );
}
