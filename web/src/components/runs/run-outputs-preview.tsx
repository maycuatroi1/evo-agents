"use client";

import { Images } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { OutputChip } from "@/components/data/output-tile";
import { Button } from "@/components/ui/button";

import type { RunOutputsState } from "./use-run-outputs";

/**
 * Under 768 px, where the run page shows a preview of the trace, the latest files the same way: "Outputs" with their
 * count and the first six as chips, then a button that opens the Outputs tab on the session's screen.
 */
export function OutputsPreview({ state, onOpenAll }: { state: RunOutputsState; onOpenAll: () => void }) {
  const t = useTranslations("runs.detail.outputs");
  const outputs = state.data?.outputs;
  const newest = useMemo(() => [...(outputs ?? [])].reverse().slice(0, 6), [outputs]);
  if (!outputs?.length) return null;
  return (
    <section aria-labelledby="run-outputs-preview" className="flex min-w-0 flex-col gap-2" data-testid="run-outputs-preview">
      <h2 id="run-outputs-preview" className="text-[15px] leading-[22px] font-semibold">
        {t("preview")} <span className="font-normal text-fg-subtle tabular-nums">{outputs.length}</span>
      </h2>
      <ul className="flex flex-wrap gap-1.5">
        {newest.map((output) => (
          <li key={output.id} className="min-w-0">
            <OutputChip output={output} onOpen={() => state.open(output, newest)} />
          </li>
        ))}
      </ul>
      <Button type="button" variant="outline" className="self-start" onClick={onOpenAll} data-testid="run-outputs-preview-open">
        <Images aria-hidden="true" />
        {t("previewOpen")}
      </Button>
    </section>
  );
}
