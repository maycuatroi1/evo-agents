import { type ReactNode, useMemo } from "react";

import { markedMarkdown } from "@/lib/marked-markdown";

import { SafeMarkdown } from "./markdown";

/**
 * Two versions of a Markdown document as one, rendered as SafeMarkdown renders the new one, with the words the new
 * version added and removed marked in place (lib/marked-markdown.ts). The document's HTML stays text, as in SafeMarkdown.
 */
export function TrackedMarkdown({
  before,
  after,
  className,
  testId = "tracked-markdown",
  variant,
}: {
  before: string;
  after: string;
  className?: string;
  testId?: string;
  variant?: "card" | "page";
}): ReactNode {
  const source = useMemo(() => markedMarkdown(before, after), [before, after]);
  return (
    <SafeMarkdown tracked className={className} testId={testId} variant={variant}>
      {source}
    </SafeMarkdown>
  );
}
