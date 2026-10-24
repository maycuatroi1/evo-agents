"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import {
  DECISION_KINDS,
  type DecisionStep,
  decisionsAbout,
  decisionSteps,
  decisionsToReview,
  decisionWhat,
  kgDecisionsQuery,
  MAX_REASON,
  stepDecision,
  takeDecision,
} from "@/lib/kg/decisions";
import { kgKeys } from "@/lib/kg/queries";
import type { DecisionKind, KgDecision } from "@/lib/kg/types";

type Write = { reason: string; base_rev: number };

/** One write of a decision: the reason it needs, the revision it names, its failure in place, a toast when done. */
function useDecisionWrite(project: string, write: (body: Write) => Promise<unknown>, done: string, onDone: () => void) {
  const t = useTranslations("kg.decisions");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [error, setError] = useState<WriteFailure | null>(null);
  const save = useMutation({
    mutationFn: write,
    onSuccess: async () => {
      setError(null);
      notify({ tone: "success", text: done });
      onDone();
      await queryClient.invalidateQueries({ queryKey: kgKeys.decisions(project) });
    },
    onError: (cause) => setError(failure(cause, { 403: t("forbidden"), 404: t("gone"), 409: t("stale"), 422: t("invalid") })),
  });
  return { save, error };
}

function Reason({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const t = useTranslations("kg.decisions");
  const id = useId();
  return (
    <>
      <Label htmlFor={id}>{t("reasonLabel")}</Label>
      <Textarea
        id={id}
        value={value}
        maxLength={MAX_REASON}
        rows={2}
        placeholder={t("reasonPlaceholder")}
        onChange={(event) => onChange(event.target.value)}
        data-testid="kg-decision-reason"
      />
    </>
  );
}

function StepForm({ project, decision, step, rev, onDone }: { project: string; decision: KgDecision; step: DecisionStep; rev: number; onDone: () => void }) {
  const t = useTranslations("kg.decisions");
  const [reason, setReason] = useState("");
  const { save, error } = useDecisionWrite(
    project,
    (body) => stepDecision(browserApi(), project, decision.number, step, body),
    t(`done.${step}`, { number: decision.number }),
    onDone,
  );
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (reason.trim()) save.mutate({ reason: reason.trim(), base_rev: rev });
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 pt-2" data-testid="kg-decision-step-form">
      <Reason value={reason} onChange={setReason} />
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <div className="flex gap-2">
        <Button type="submit" size="sm" variant="secondary" disabled={!reason.trim()} aria-busy={save.isPending} data-testid="kg-decision-step-save">
          {t(`steps.${step}`)}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onDone}>
          {t("cancel")}
        </Button>
      </div>
    </form>
  );
}

function DecisionRow({ project, decision, rev, canDecide }: { project: string; decision: KgDecision; rev: number; canDecide: boolean }) {
  const t = useTranslations("kg.decisions");
  const [step, setStep] = useState<DecisionStep | null>(null);
  const first = decision.history[0];
  const last = decision.history[decision.history.length - 1];
  return (
    <li className="flex flex-col gap-1 border-t py-3 first:border-t-0" data-testid="kg-decision" data-number={decision.number}>
      <div className="flex flex-wrap items-center gap-2">
        <Tag>{t(`kinds.${decision.kind}`)}</Tag>
        <Tag data-testid="kg-decision-state">{t(`states.${decision.state}`)}</Tag>
        <code className="font-mono text-xs break-all">{decisionWhat(decision)}</code>
      </div>
      {first ? <p className="text-sm text-pretty">{first.reason}</p> : null}
      {last ? (
        <p className="text-xs text-muted-foreground">
          {t(`actions.${last.action}`, { by: last.by, number: decision.number })} <When value={last.at} short />
        </p>
      ) : null}
      {canDecide && step ? (
        <StepForm project={project} decision={decision} step={step} rev={rev} onDone={() => setStep(null)} />
      ) : canDecide ? (
        <div className="flex gap-2">
          {decisionSteps(decision).map((s) => (
            <Button key={s} size="sm" variant="outline" onClick={() => setStep(s)} data-testid={`kg-decision-${s}`}>
              {t(`steps.${s}`)}
            </Button>
          ))}
        </div>
      ) : null}
    </li>
  );
}

function TakeForm({ project, nodeId, rev }: { project: string; nodeId: string; rev: number }) {
  const t = useTranslations("kg.decisions");
  const relId = useId();
  const toId = useId();
  const kindId = useId();
  const [kind, setKind] = useState<DecisionKind>("deprecate");
  const [rel, setRel] = useState("");
  const [to, setTo] = useState("");
  const [reason, setReason] = useState("");
  const reset = () => {
    setRel("");
    setTo("");
    setReason("");
  };
  const { save, error } = useDecisionWrite(
    project,
    (body) =>
      takeDecision(browserApi(), project, {
        ...body,
        kind,
        src: nodeId,
        rel: kind === "merge" ? null : rel.trim() || null,
        dst: to.trim() || null,
      }),
    t("done.take"),
    reset,
  );
  const needsTo = kind !== "deprecate" || rel.trim() !== "";
  const needsRel = kind === "link" || (kind === "deprecate" && to.trim() !== "");
  const ready = reason.trim() !== "" && (!needsTo || to.trim() !== "") && (!needsRel || rel.trim() !== "");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (ready) save.mutate({ reason: reason.trim(), base_rev: rev });
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 border-t pt-3" data-testid="kg-decision-take-form">
      <Label htmlFor={kindId}>{t("kindLabel")}</Label>
      <NativeSelect id={kindId} value={kind} onChange={(event) => setKind(event.target.value as DecisionKind)} data-testid="kg-decision-kind">
        {DECISION_KINDS.map((k) => (
          <NativeSelectOption key={k} value={k}>
            {t(`kinds.${k}`)}
          </NativeSelectOption>
        ))}
      </NativeSelect>
      <p className="text-xs text-muted-foreground">{t(`hints.${kind}`)}</p>
      {kind !== "merge" ? (
        <>
          <Label htmlFor={relId}>{t("relLabel")}</Label>
          <Input id={relId} value={rel} onChange={(event) => setRel(event.target.value)} data-testid="kg-decision-rel" />
        </>
      ) : null}
      <Label htmlFor={toId}>{t("toLabel")}</Label>
      <Input id={toId} value={to} onChange={(event) => setTo(event.target.value)} className="font-mono" data-testid="kg-decision-to" />
      <Reason value={reason} onChange={setReason} />
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <Button type="submit" size="sm" variant="secondary" className="self-start" disabled={!ready} aria-busy={save.isPending} data-testid="kg-decision-take">
        {t(`take.${kind}`)}
      </Button>
    </form>
  );
}

function DecisionList({ project, decisions, rev, canDecide, empty, testId }: { project: string; decisions: KgDecision[]; rev: number; canDecide: boolean; empty: string; testId: string }) {
  if (decisions.length === 0) {
    return <p className="text-sm text-muted-foreground" data-testid={`${testId}-empty`}>{empty}</p>;
  }
  return (
    <ul data-testid={testId}>
      {decisions.map((d) => (
        <DecisionRow key={d.number} project={project} decision={d} rev={rev} canDecide={canDecide} />
      ))}
    </ul>
  );
}

function Panel({ title, testId, children }: { title: string; testId: string; children: React.ReactNode }) {
  return (
    <Card role="region" aria-label={title} data-testid={testId}>
      <CardHeader>
        <CardTitle className="text-[15px] leading-[22px] font-semibold">
          <h2>{title}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">{children}</CardContent>
    </Card>
  );
}

/** The decisions about a node, and for a writer the forms to deprecate it, merge it into another node or link it. */
export function NodeDecisions({ project, nodeId }: { project: string; nodeId: string }) {
  const t = useTranslations("kg.decisions");
  const query = useQuery(kgDecisionsQuery(browserApi, project));
  if (!query.isSuccess) return null;
  const { decisions, rev, can_decide } = query.data;
  return (
    <Panel title={t("title")} testId="kg-node-decisions">
      <DecisionList project={project} decisions={decisionsAbout(decisions, [nodeId])} rev={rev} canDecide={can_decide} empty={t("none")} testId="kg-decision-list" />
      {can_decide ? <TakeForm project={project} nodeId={nodeId} rev={rev} /> : <p className="text-xs text-muted-foreground">{t("readerNote")}</p>}
    </Panel>
  );
}

/** The decisions about a cluster's members. */
export function ClusterDecisions({ project, ids }: { project: string; ids: readonly string[] }) {
  const t = useTranslations("kg.decisions");
  const query = useQuery(kgDecisionsQuery(browserApi, project));
  if (!query.isSuccess) return null;
  const { decisions, rev, can_decide } = query.data;
  return (
    <Panel title={t("clusterTitle")} testId="kg-cluster-decisions">
      <DecisionList project={project} decisions={decisionsAbout(decisions, ids)} rev={rev} canDecide={can_decide} empty={t("none")} testId="kg-decision-list" />
    </Panel>
  );
}

/** The review page's share: the agents' proposals a writer accepts or rejects, and the orphans to undo. */
export function ReviewDecisions({ project }: { project: string }) {
  const t = useTranslations("kg.decisions");
  const query = useQuery(kgDecisionsQuery(browserApi, project));
  if (!query.isSuccess) return null;
  const { rev, can_decide } = query.data;
  const { proposed, orphans } = decisionsToReview(query.data.decisions);
  return (
    <Panel title={t("reviewTitle")} testId="kg-review-decisions">
      <h3 className="text-sm font-medium">{t("proposed")}</h3>
      <DecisionList project={project} decisions={proposed} rev={rev} canDecide={can_decide} empty={t("noProposals")} testId="kg-decision-proposals" />
      <h3 className="text-sm font-medium">{t("orphans")}</h3>
      <p className="text-xs text-muted-foreground">{t("orphansNote")}</p>
      <DecisionList project={project} decisions={orphans} rev={rev} canDecide={can_decide} empty={t("noOrphans")} testId="kg-decision-orphans" />
    </Panel>
  );
}
