"use client";

import { ArrowRight, ChevronLeft, ChevronRight, CircleCheck, Clock, X } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Button } from "@/components/ui/button";
import type { StepCriterion } from "@/lib/plan-graph/criteria";
import type { GraphEdge, GraphNode, PlanGraph } from "@/lib/plan-graph/layout";
import { type PlanStep, stepLabel } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { formatDay } from "./depends-on";
import { stepHref } from "./links";
import { Prose, Verbatim } from "./prose";
import { BlockingBadge, StepStatusBadge, StepStatusIcon } from "./status";

/**
 * The inspector of the Graph view: what a step's page says about the selected step, read only and without its runs, so
 * a reader can follow the plan without leaving the graph. Every dependency the step declares is listed, drawn or not,
 * each a button that selects that step; the plan's criteria the step names are quoted. Nothing here runs or changes
 * the step: the link at the foot opens its page, where that happens.
 */

/** Evidence shows this many lines until "Show all". */
const EVIDENCE_LINES = 3;

export type InspectorProps = {
  graph: PlanGraph;
  node: GraphNode;
  project: string;
  planId: string;
  criteria: StepCriterion[];
  /** Selects another step and brings it into view. */
  onSelect: (key: string) => void;
  onClose: () => void;
  /** The heading: a plain h3 beside the canvas, the sheet's title below 1024 px. */
  heading: (children: ReactNode) => ReactNode;
};

/** A button that selects a step of the plan, with its status icon; a key the plan lacks is plain text. */
function StepButton({ step, at, onSelect, children }: { step: PlanStep; at: string; onSelect: (key: string) => void; children?: ReactNode }) {
  const t = useTranslations("plans.graph");
  return (
    <button
      type="button"
      className="inline-flex cursor-pointer items-center gap-1 rounded-sm font-mono text-sm text-brand underline-offset-4 hover:underline max-md:min-h-11 max-md:min-w-11 max-md:px-1.5"
      aria-label={t("selectStep", { key: step.key, title: stepLabel(step) || t("untitled") })}
      onClick={() => onSelect(step.key)}
      data-testid={`inspector-${at}`}
      data-step={step.key}
    >
      <StepStatusIcon group={step.group} raw={step.rawStatus} className="[&_svg]:size-3.5" />
      {step.key}
      {children}
    </button>
  );
}

function Dependencies({ edges, byKey, onSelect }: { edges: GraphEdge[]; byKey: Map<string, PlanStep>; onSelect: (key: string) => void }) {
  const t = useTranslations("plans.graph");
  const s = useTranslations("plans.step");
  if (edges.length === 0) return <span className="text-sm text-muted-foreground">{s("noDependencies")}</span>;
  return (
    <ul className="flex flex-col gap-1">
      {edges.map((edge) => {
        const target = byKey.get(edge.from);
        const hint = edge.kind === "implied" ? t("kindImplied") : edge.kind === "cycle" ? t("kindCycle") : null;
        return (
          <li key={edge.from} className="flex flex-wrap items-center gap-x-2" data-kind={edge.kind}>
            {target ? (
              <StepButton step={target} at="dependency" onSelect={onSelect} />
            ) : (
              <span className="font-mono text-sm text-muted-foreground" data-testid="inspector-dependency" data-step={edge.from}>
                {edge.from}
              </span>
            )}
            {target ? <span className="min-w-0 truncate text-xs text-muted-foreground">{stepLabel(target)}</span> : null}
            {hint || !target ? (
              <span className="text-xs text-muted-foreground italic">{target ? hint : s("unknownStep")}</span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function Section({ title, children, testId }: { title: string; children: ReactNode; testId: string }) {
  return (
    <section className="flex min-w-0 flex-col gap-1.5" data-testid={testId}>
      <h4 className="text-xs font-medium text-muted-foreground">{title}</h4>
      {children}
    </section>
  );
}

function Evidence({ evidence }: { evidence: string }) {
  const t = useTranslations("plans.graph");
  const [all, setAll] = useState(false);
  const lines = evidence.replace(/\n+$/, "").split("\n");
  const long = lines.length > EVIDENCE_LINES;
  return (
    <div className="flex flex-col items-start gap-1">
      <Verbatim testId="inspector-evidence-text" className="w-full border-success/30 bg-success-soft/40 text-xs">
        {all || !long ? lines.join("\n") : lines.slice(0, EVIDENCE_LINES).join("\n")}
      </Verbatim>
      {long ? (
        <Button type="button" variant="ghost" size="sm" aria-expanded={all} onClick={() => setAll((value) => !value)} data-testid="inspector-evidence-toggle">
          {all ? t("showFewerLines") : t("showAllLines", { count: lines.length })}
        </Button>
      ) : null}
    </div>
  );
}

export function StepInspector({ graph, node, project, planId, criteria, onSelect, onClose, heading }: InspectorProps) {
  const t = useTranslations("plans.graph");
  const s = useTranslations("plans.step");
  const format = useFormatter();
  const { step } = node;
  const steps = graph.nodes.map((n) => n.step);
  const byKey = new Map<string, PlanStep>();
  for (const one of steps) if (!byKey.has(one.key)) byKey.set(one.key, one);
  const previous = steps[step.index - 1] ?? null;
  const next = steps[step.index + 1] ?? null;
  const declared = graph.edges.filter((edge) => edge.toIndex === step.index);
  const waiting = graph.nodes.filter((other) => other.step.dependsOn.includes(step.key)).map((other) => other.step);
  const title = stepLabel(step) || t("untitled");
  const nav = "max-md:size-11";

  return (
    <div className="flex min-w-0 flex-col" data-testid="inspector-body" data-step={step.key}>
      <div className="flex items-center gap-1.5 border-b px-3 py-2">
        <span className="font-mono text-sm font-semibold tabular-nums" data-testid="inspector-key">
          {step.key}
        </span>
        <StepStatusBadge group={step.group} raw={step.rawStatus} testId="inspector-status" />
        <div className="ml-auto flex items-center gap-0.5">
          <Button type="button" variant="ghost" size="icon-sm" className={nav} disabled={!previous}
            aria-label={previous ? t("previousStep", { key: previous.key }) : t("noPrevious")}
            onClick={() => previous && onSelect(previous.key)} data-testid="inspector-previous">
            <ChevronLeft aria-hidden="true" />
          </Button>
          <Button type="button" variant="ghost" size="icon-sm" className={nav} disabled={!next}
            aria-label={next ? t("nextStep", { key: next.key }) : t("noNext")}
            onClick={() => next && onSelect(next.key)} data-testid="inspector-next">
            <ChevronRight aria-hidden="true" />
          </Button>
          <Button type="button" variant="ghost" size="icon-sm" className={nav} aria-label={t("close")} onClick={onClose} data-testid="inspector-close">
            <X aria-hidden="true" />
          </Button>
        </div>
      </div>
      <div className="flex flex-col gap-4 px-3 py-3">
        {heading(title)}
        {node.ready ? (
          <p className="flex items-start gap-2 rounded-md bg-success-soft px-2.5 py-2 text-sm text-success" data-testid="inspector-ready">
            <CircleCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            {t("readyCallout")}
          </p>
        ) : node.waitingOn.length ? (
          <p className="flex items-start gap-2 rounded-md bg-surface-sunken px-2.5 py-2 text-sm" data-testid="inspector-waits-on">
            <Clock className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            {t("waitsOn", { keys: node.waitingOn.join(", "), count: node.waitingOn.length })}
          </p>
        ) : null}
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2.5 text-sm" data-testid="inspector-facts">
          <dt className="text-muted-foreground">{s("repo")}</dt>
          <dd className="min-w-0 font-mono [overflow-wrap:anywhere]">{step.repo ?? <span className="text-muted-foreground">-</span>}</dd>
          <dt className="text-muted-foreground">{s("doneAt")}</dt>
          <dd className="tabular-nums">
            {step.doneAt ? <time dateTime={step.doneAt}>{formatDay(format, step.doneAt)}</time> : <span className="text-muted-foreground">-</span>}
          </dd>
          <dt className="text-muted-foreground">{s("blockingLabel")}</dt>
          <dd>
            <BlockingBadge blocking={step.blocking} showFalse />
          </dd>
          <dt className="text-muted-foreground">{s("dependsOn")}</dt>
          <dd className="min-w-0" data-testid="inspector-depends-on">
            <Dependencies edges={declared} byKey={byKey} onSelect={onSelect} />
          </dd>
          <dt className="text-muted-foreground">{t("waitingFor")}</dt>
          <dd className="flex min-w-0 flex-wrap gap-x-2.5 gap-y-1" data-testid="inspector-dependents">
            {waiting.length ? (
              waiting.map((other) => <StepButton key={other.index} step={other} at="dependent" onSelect={onSelect} />)
            ) : (
              <span className="text-muted-foreground">{s("noDependents")}</span>
            )}
          </dd>
        </dl>
        <Section title={s("what")} testId="inspector-what">
          {step.what ? <Prose className="text-[13px]">{step.what}</Prose> : <p className="text-sm text-muted-foreground italic">{s("noWhat")}</p>}
        </Section>
        <Section title={s("verify")} testId="inspector-verify">
          {step.verify ? <Verbatim className="text-xs">{step.verify}</Verbatim> : <p className="text-sm text-muted-foreground italic">{s("noVerify")}</p>}
        </Section>
        <Section title={s("evidence")} testId="inspector-evidence">
          {step.evidence ? (
            <Evidence evidence={step.evidence} />
          ) : (
            <p className="text-sm text-muted-foreground italic">{step.group === "done" ? s("noEvidenceDone") : s("noEvidence")}</p>
          )}
        </Section>
        {step.note ? (
          <Section title={s("note")} testId="inspector-note">
            <Prose className="text-[13px]">{step.note}</Prose>
          </Section>
        ) : null}
        {criteria.length ? (
          <Section title={t("criteria")} testId="inspector-criteria">
            <ul className="flex flex-col gap-2">
              {criteria.map((criterion, i) => (
                <li key={`${criterion.id ?? "own"}-${i}`} className="flex gap-2 text-[13px]" data-criterion={criterion.id ?? ""}>
                  {criterion.id ? <span className="shrink-0 font-mono text-xs leading-5 font-medium">{criterion.id}</span> : null}
                  <span className={cn("min-w-0 whitespace-pre-line [overflow-wrap:anywhere]", criterion.text === null && "text-muted-foreground italic")}>
                    {criterion.text ?? t("criterionUnknown")}
                  </span>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
        <Link
          href={stepHref(project, planId, step.key)}
          className="inline-flex w-fit items-center gap-1.5 rounded-sm text-sm font-medium text-brand underline-offset-4 hover:underline max-md:min-h-11"
          data-testid="inspector-open-page"
        >
          {t("openStepPage")}
          <ArrowRight className="size-3.5" aria-hidden="true" />
        </Link>
      </div>
    </div>
  );
}
