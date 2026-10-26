"use client";

import { useQuery } from "@tanstack/react-query";
import { Info, RotateCcw, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { agentQuery } from "@/lib/agent-queries";
import { MAX_ALLOW, validItem } from "@/lib/agent-rules";
import { type Agent, allowed } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";

import { AxisField } from "./axis-field";
import { useAgentAdmin, useAgentFailure, useEditAgent } from "./hooks";
import { type AxisDraft, draftOf, permissionsBody, sameAxis } from "./model";
import { agentOptionsQuery } from "./options";
import { ReadOnlyNote } from "./parts";

/**
 * Where an agent's runs may go, on its own (web/DESIGN.md, Chats, the agent sheet): its worker axis, which a chat
 * opens from the agent's menu. A run under the agent, a chat run too, goes to a worker of the person who hands it the
 * work (or one shared with them) that the axis lets through: any, or only the workers, labels and pools it names, a
 * pool covering each of its nodes, those that join later too. An admin of the project changes it here, as a new
 * version of the agent against the one shown (`if_version`, so an edit made meanwhile is refused, not overwritten);
 * everyone else, and anyone for the project's default agent (which keeps every permission) or a deleted one, reads it.
 */
export function AgentWorkersPanel({ project, handle }: { project: string; handle: string }) {
  const state = useHubQuery(agentQuery(browserApi, project, handle));
  // Kept above the form, which starts again from each version the hub holds: a refused save stays said once the
  // agent as it is now replaces the draft.
  const [failure, setFailure] = useState<WriteFailure | null>(null);
  return (
    <QueryView state={state} loading={<Skeleton className="h-56 w-full rounded-md" />}>
      {(agent) => <WorkersForm key={agent.version} project={project} agent={agent} failure={failure} onFailure={setFailure} />}
    </QueryView>
  );
}

function WorkersForm({
  project,
  agent,
  failure,
  onFailure: setFailure,
}: {
  project: string;
  agent: Agent;
  failure: WriteFailure | null;
  onFailure: (failure: WriteFailure | null) => void;
}) {
  const t = useTranslations("agents.workers");
  const admin = useAgentAdmin(project);
  const editable = admin && !agent.is_default && agent.deleted_at === null;
  const saved = draftOf(agent).permissions.worker;
  const [draft, setDraft] = useState<AxisDraft>(saved);
  const [tried, setTried] = useState(false);
  const options = useQuery({ ...agentOptionsQuery(browserApi, project), enabled: editable && draft.mode === "allowlist" });
  const edit = useEditAgent(project, agent.handle);
  const failureOf = useAgentFailure();
  const changed = !sameAxis(draft, saved);
  const problem = draft.mode === "all" ? undefined : draft.allow.length > MAX_ALLOW ? "tooMany" : draft.allow.some((item) => !validItem("worker", item)) ? "item" : undefined;

  const save = async () => {
    setTried(true);
    if (problem || !changed) return;
    setFailure(null);
    const permissions = permissionsBody({ ...draftOf(agent).permissions, worker: draft });
    try {
      await edit.mutateAsync({ permissions, if_version: agent.version });
    } catch (error) {
      setFailure(failureOf(error, { 409: t("conflict") }));
      return;
    }
    notify({ tone: "success", text: t("saved", { handle: agent.handle }), description: t("savedText") });
  };

  return (
    <div className="flex flex-col gap-4" data-testid="agent-workers" data-handle={agent.handle} data-editable={editable}>
      <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground">{t("lead", { handle: agent.handle })}</p>
      {editable ? null : <ReadOnly project={project} agent={agent} admin={admin} />}
      {editable ? (
        <>
          <AxisField
            axis="worker"
            value={draft}
            onChange={(next) => {
              setDraft(next);
              setFailure(null);
            }}
            options={options.data?.byAxis.worker ?? []}
            loading={options.isPending && draft.mode === "allowlist"}
            failed={options.data?.failed.includes("worker") ?? options.isError}
            problem={tried ? problem : undefined}
            disabled={edit.isPending}
          />
          {failure ? <InlineError text={failure.text} detail={failure.detail} requestId={failure.requestId} /> : null}
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!changed || edit.isPending}
              onClick={() => {
                setDraft(saved);
                setTried(false);
                setFailure(null);
              }}
              data-testid="agent-workers-reset"
            >
              <RotateCcw aria-hidden="true" />
              {t("reset")}
            </Button>
            <Button type="button" size="sm" disabled={!changed} busy={edit.isPending} onClick={() => void save()} data-testid="agent-workers-save">
              {edit.isPending ? t("saving") : t("save")}
            </Button>
          </div>
        </>
      ) : (
        <Summary agent={agent} />
      )}
    </div>
  );
}

/** Why the visitor reads the axis here and cannot change it. */
function ReadOnly({ project, agent, admin }: { project: string; agent: Agent; admin: boolean }) {
  const t = useTranslations("agents.workers");
  if (!admin) return <ReadOnlyNote project={project} />;
  return (
    <p role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-pretty text-muted-foreground" data-testid="agent-workers-fixed">
      <Info className="mt-px size-4 shrink-0" aria-hidden="true" />
      <span>{agent.is_default ? t("defaultNote") : t("deletedNote")}</span>
    </p>
  );
}

/** The axis as it stands: any worker, nothing, or the names, labels and pools it allows as chips. */
function Summary({ agent }: { agent: Agent }) {
  const t = useTranslations("agents.workers");
  const tAxis = useTranslations("agents.axes");
  const allow = allowed(agent.permissions, "worker");
  return (
    <div className="flex flex-col gap-1.5 rounded-md border px-3 py-3 sm:px-4" data-testid="agent-workers-summary" data-mode={allow === null ? "all" : "allowlist"}>
      <h3 className="text-sm font-medium">{tAxis("worker.title")}</h3>
      {allow === null ? (
        <p className="text-sm text-muted-foreground">{tAxis("worker.all")}</p>
      ) : allow.length === 0 ? (
        <p className="flex items-start gap-1.5 text-sm font-medium text-attention">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {t("nothing")}
        </p>
      ) : (
        <ul className="flex min-w-0 flex-wrap gap-1.5" aria-label={t("allowed")}>
          {allow.map((value) => (
            <li key={value} className="inline-flex h-[22px] max-w-full items-center rounded-xs bg-surface-sunken px-1.5 font-mono text-xs text-foreground" data-value={value}>
              <span className="truncate">{value}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
