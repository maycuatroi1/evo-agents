"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, FlaskConical } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { chatAgentsQuery } from "@/lib/chat-keys";
import { whoamiQuery } from "@/lib/queries";
import type { TraceSummary } from "@/lib/systems";
import { systemTracesQuery } from "@/lib/systems-queries";

import type { DesignerSlots } from "./slots";
import { TraceView } from "./trace-view";

/** Leeway for the clocks of the browser and the hub, when telling the traces of this test from earlier ones. */
const SKEW_MS = 60_000;

/** The newest trace the visitor opened since the test started, or null. */
export function testTrace(traces: readonly TraceSummary[], login: string | null, since: number): TraceSummary | null {
  return traces.find((trace) => trace.opened_by === login && Date.parse(trace.started_at) >= since - SKEW_MS) ?? null;
}

type Props = {
  project: string;
  slug: string;
  /** The handle of the entry agent of the saved revision. */
  entry: string;
  revision: number;
  dirty: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  slots: DesignerSlots;
};

/**
 * Test: the chat with the system's entry agent in a sheet (the chat home's frame, interactive answers included), so a
 * message goes in as a person's would and opens a trace of the saved revision. Once that trace exists, View trace
 * shows its runs and tool calls; the sheet keeps the chat a click away.
 */
export function TestSheet({ project, slug, entry, revision, dirty, open, onOpenChange, slots }: Props) {
  const t = useTranslations("systems.test");
  const [since] = useState(() => Date.now());
  const [viewing, setViewing] = useState<number | null>(null);
  const me = useQuery(whoamiQuery(browserApi));
  const agents = useQuery({ ...chatAgentsQuery(browserApi), enabled: open });
  const traces = useQuery({ ...systemTracesQuery(browserApi, project, slug), enabled: open, refetchInterval: open ? 3000 : false });
  const agent = agents.data?.find((one) => one.handle === entry && one.project === project) ?? null;
  const trace = testTrace(traces.data ?? [], me.data?.login ?? null, since);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-2xl" data-testid="system-test-sheet">
        <div className="flex flex-col gap-1 border-b px-4 py-3 pr-12">
          <SheetTitle className="flex items-center gap-2 text-base font-semibold">
            <FlaskConical className="size-4 text-muted-foreground" aria-hidden="true" />
            {t("title", { handle: entry })}
          </SheetTitle>
          <SheetDescription className="text-pretty">{dirty ? t("unsaved", { revision }) : t("description", { revision })}</SheetDescription>
          <div className="flex flex-wrap items-center gap-2 pt-1" aria-live="polite">
            {trace ? (
              <Button type="button" size="sm" variant={viewing === trace.id ? "secondary" : "outline"} onClick={() => setViewing(viewing === trace.id ? null : trace.id)} data-testid="system-test-trace">
                <Activity aria-hidden="true" />
                {viewing === trace.id ? t("backToChat") : t("viewTrace", { runs: trace.runs })}
              </Button>
            ) : (
              <span className="text-xs text-muted-foreground" data-testid="system-test-waiting">
                {t("noTrace")}
              </span>
            )}
          </div>
        </div>
        {viewing !== null ? (
          <TraceView project={project} slug={slug} id={viewing} onBack={() => setViewing(null)} />
        ) : agents.isPending ? (
          <Skeleton className="m-4 h-40" />
        ) : agent ? (
          <div className="flex min-h-0 flex-1 flex-col" data-testid="system-test-chat">
            <slots.TestChat agent={agent} />
          </div>
        ) : (
          <StatePanel icon={FlaskConical} title={t("noChatTitle")} description={t("noChat", { handle: entry })} className="m-4 shadow-none" testId="system-test-no-chat" />
        )}
      </SheetContent>
    </Sheet>
  );
}
