"use client";

import { Bot, CircleAlert, Database, type LucideIcon, MessageSquare, Network, Wrench } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { chatHref } from "@/lib/chat-keys";
import { runPageHref } from "@/lib/loop-queries";
import type { TraceSpan } from "@/lib/systems";
import { type Bar, treeOrder } from "@/lib/systems-observe";
import { cn } from "@/lib/utils";

const MESSAGE_TOOLS = new Set(["agent_ask", "chat_mention", "agent_forward"]);
const DATA_SOURCE = "gen_ai.data_source.id";
const KG_RESULTS = "evo.kg.results";

/** What a span is, for its icon and its words: a run of an agent, a kg query, a message to another agent, else a tool. */
export function spanKind(span: TraceSpan): "run" | "kg" | "message" | "escalate" | "tool" {
  if (span.name === "invoke_agent") return "run";
  if (span.tool?.startsWith("kg_")) return "kg";
  if (span.tool && MESSAGE_TOOLS.has(span.tool)) return "message";
  if (span.tool?.includes("escalat")) return "escalate";
  return "tool";
}

const KIND_ICON: Record<ReturnType<typeof spanKind>, LucideIcon> = { run: Bot, kg: Database, message: MessageSquare, escalate: Network, tool: Wrench };

const BAR_TONE: Record<string, string> = {
  running: "bg-running motion-safe:animate-pulse",
  ok: "bg-success",
  error: "bg-danger",
  refused: "bg-danger",
};

type Props = { spans: readonly TraceSpan[]; bars: ReadonlyMap<number, Bar>; chatId: number | null };

/**
 * The waterfall of a trace: its spans in the order of their tree, each under the span that started it, with a bar on
 * the trace's time and what it was: the run of an agent (its node, model, tokens and cost, a link to the run), the
 * message it sent to another agent (a link to it in the chat), the knowledge graph it queried and how many results it
 * read, the tool it called. A span off the design is marked in the warning colour, a refusal says why.
 */
export function TraceWaterfall({ spans, bars, chatId }: Props) {
  const t = useTranslations("systems.traceView");
  const format = useFormatter();
  const usd = (value: number) => format.number(value, { style: "currency", currency: "USD", maximumFractionDigits: 4 });
  return (
    <ol className="flex flex-col divide-y rounded-md border" aria-label={t("waterfall")} data-testid="system-waterfall">
      {treeOrder(spans).map(({ span, depth }) => {
        const kind = spanKind(span);
        const Icon = span.status === "refused" ? CircleAlert : KIND_ICON[kind];
        const bar = bars.get(span.id) ?? { left: 0, width: 100 };
        const graph = span.attributes?.[DATA_SOURCE];
        const results = span.attributes?.[KG_RESULTS];
        const title = kind === "run" ? `@${span.agent}` : kind === "kg" ? t("kgQuery", { tool: span.tool ?? "", graph: typeof graph === "string" ? graph : "default" }) : t("toolTo", { tool: span.tool ?? span.name, to: span.to_node ?? "" });
        const details = [
          t(`status.${span.status === "running" || span.status === "ok" || span.status === "error" || span.status === "refused" ? span.status : "ok"}`),
          span.seconds !== null && span.seconds !== undefined ? t("seconds", { seconds: format.number(span.seconds, { maximumFractionDigits: 1 }) }) : null,
          kind === "run" && span.model ? span.model : null,
          kind === "run" && span.input_tokens + span.output_tokens > 0 ? t("tokens", { input: span.input_tokens, output: span.output_tokens }) : null,
          kind === "run" && span.usd ? usd(span.usd) : null,
          kind === "kg" && typeof results === "number" ? t("results", { count: results }) : null,
        ].filter(Boolean);
        return (
          <li key={span.id} className="grid gap-x-4 gap-y-1 px-3 py-2 text-[13px] md:grid-cols-[minmax(0,18rem)_1fr]" data-testid="system-waterfall-span" data-span={span.id} data-kind={kind} data-status={span.status} data-node={span.node ?? ""} data-off-design={span.off_design ? "true" : "false"}>
            <div className="flex min-w-0 items-start gap-2" style={{ paddingLeft: `${Math.min(depth, 8) * 0.875}rem` }}>
              <Icon className={cn("mt-0.5 size-3.5 shrink-0", span.status === "refused" || span.status === "error" ? "text-danger" : "text-muted-foreground")} aria-hidden="true" />
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className="truncate font-medium" title={title}>
                  {title}
                  {kind === "run" && span.node ? <span className="font-normal text-muted-foreground">{` ${t("asNode", { node: span.node })}`}</span> : null}
                </span>
                <span className="text-xs text-muted-foreground tabular-nums">
                  {details.join(", ")}
                  {span.off_design ? <span className="text-attention">{`, ${t("offDesign")}`}</span> : null}
                </span>
                {span.reason ? <span className="text-xs text-pretty text-muted-foreground">{span.reason}</span> : null}
                {span.content_withheld ? <span className="text-xs text-muted-foreground">{t("withheld", { project: span.project ?? "" })}</span> : null}
                <span className="flex flex-wrap gap-x-3 text-xs">
                  {span.run_id && span.project ? (
                    <Link href={runPageHref(span.project, span.run_id)} className="text-brand hover:underline">
                      {t("run", { id: span.run_id })}
                    </Link>
                  ) : null}
                  {span.message_id && chatId ? (
                    <Link href={chatHref(chatId, span.message_id)} className="text-brand hover:underline">
                      {t("message")}
                    </Link>
                  ) : null}
                </span>
              </div>
            </div>
            <div className="relative h-5 self-center rounded-sm bg-muted" aria-hidden="true">
              <span
                className={cn("absolute inset-y-0.5 rounded-sm", BAR_TONE[span.status] ?? "bg-neutral-solid", span.off_design && "outline-2 outline-attention outline-dashed")}
                style={{ left: `${bar.left}%`, width: `${bar.width}%` }}
                data-testid="system-waterfall-bar"
              />
            </div>
          </li>
        );
      })}
    </ol>
  );
}
