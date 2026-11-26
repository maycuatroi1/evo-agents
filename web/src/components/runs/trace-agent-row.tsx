"use client";

import { Bot, Brain, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";

import { SafeMarkdown } from "@/components/data/markdown";

import { LinkedText } from "./links";
import { holdsStepReport } from "./step-report-start";
import type { TraceItem } from "./trace-model";
import { useTraceDuration } from "./trace-look";
import { StepReportMessage } from "./trace-report-lazy";
import { Row, Who } from "./trace-row";
import type { TraceContext } from "./trace-rows";

/** The agent's words in the trace (agent-trace.tsx): its thinking folded, its message as Markdown, a step's report as a card. */

const reportProse = (text: string) => <SafeMarkdown className="text-[13px] leading-5" testId="trace-report-text">{text}</SafeMarkdown>;
const message = (text: string) => <SafeMarkdown className="leading-[21px]" testId="trace-message">{text}</SafeMarkdown>;

export function AgentRow({ item, context }: { item: Extract<TraceItem, { type: "agent" }>; context: TraceContext }) {
  const t = useTranslations("runs.detail.trace");
  const short = useTraceDuration();
  const thought = item.thought;
  return (
    <Row icon={Bot} look="agent" at={item.at} head={<Who>{t("agent")}</Who>}>
      <div className="flex min-w-0 flex-col gap-2">
        {thought ? (
          <details className="group/thought min-w-0 text-[13px] leading-[19px] text-muted-foreground" data-testid="trace-thought">
            <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-fg-subtle select-none hover:text-foreground max-md:min-h-11 [&::-webkit-details-marker]:hidden">
              <Brain className="size-3.5" aria-hidden="true" />
              {thought.ms >= 1000 ? t("thought", { duration: short(thought.ms) }) : t("thoughtBrief")}
              <ChevronRight className="size-3 transition-transform duration-[var(--transition-duration-base)] group-open/thought:rotate-90" aria-hidden="true" />
            </summary>
            <div className="mt-2 border-l-2 border-border pl-3 break-words whitespace-pre-wrap">
              <LinkedText text={thought.text} />
            </div>
          </details>
        ) : null}
        {/* A step's report, which Markdown would fold into one paragraph, shows as a card between the words around it. */}
        {holdsStepReport(item.text) ? (
          <StepReportMessage text={item.text} message={message} prose={reportProse} webOf={(branch) => context.webOfBranch?.(branch) ?? null} />
        ) : item.text ? (
          message(item.text)
        ) : null}
      </div>
    </Row>
  );
}
