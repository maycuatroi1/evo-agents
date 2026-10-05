"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Ban,
  CircleCheck,
  CircleHelp,
  CircleMinus,
  GitBranch,
  Info,
  Loader2,
  type LucideIcon,
  Pause,
  Play,
  Power,
  ShieldAlert,
  TriangleAlert,
  WifiOff,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { NoticeArea, type Notice, useNotice } from "@/components/admin/notice";
import { WorkerRuns } from "@/components/runs/worker-runs";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { Ago } from "./ago";
import { ChipList, WorkerStatusBadge } from "./badges";
import { ConfirmByName } from "./confirm-by-name";
import { HeartbeatStrip } from "./heartbeat-strip";
import { useRecordPrefetched, useWorkerFailure, useWorkerWrite } from "./hooks";
import { readCheckouts, readRuntimes, type RuntimeInfo, workerView } from "./model";
import { changeWorker, type Worker, type WorkerAction, workerKeys, workerQuery } from "./queries";

/**
 * One worker: its runtimes, scope and checkouts, the last hour of heartbeats, its latest runs, and drain, resume and
 * revoke.
 */
export function WorkerDetail({ id, initialError }: { id: number; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("workers.detail");
  const state = useHubQuery(workerQuery(browserApi, id), initialError);
  const { notice, show, clear } = useNotice();
  useRecordPrefetched(workerKeys.one(id), (data) => [data as Worker]);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle")} description={t("notFoundDescription")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(worker) => <WorkerPage worker={worker} notice={notice} onNotice={show} onDismiss={clear} />}
    </QueryView>
  );
}

function WorkerPage({
  worker,
  notice,
  onNotice,
  onDismiss,
}: {
  worker: Worker;
  notice: Notice | null;
  onNotice: (notice: Notice) => void;
  onDismiss: () => void;
}) {
  const t = useTranslations("workers.detail");
  const format = useFormatter();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const view = workerView(worker);
  const isOwner = me?.login === worker.owner;
  const registered = format.dateTime(new Date(worker.created_at), { dateStyle: "medium" });

  return (
    <div className="flex flex-col gap-6" data-testid="worker-detail" data-worker-id={worker.id}>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <span className="font-mono [overflow-wrap:anywhere]">{worker.name}</span>
            <WorkerStatusBadge view={view} className="h-6 text-sm" />
          </span>
        }
        description={t("facts", {
          os: worker.os,
          arch: worker.arch,
          hostname: worker.hostname,
          owner: worker.owner,
          version: worker.agent_version,
          date: registered,
        })}
        meta={<WorkerActions worker={worker} isOwner={isOwner} onNotice={onNotice} />}
      />
      <NoticeArea notice={notice} onDismiss={onDismiss} />
      <StatusNote worker={worker} isOwner={isOwner} admin={Boolean(me?.admin)} />
      <div className="grid gap-4 lg:grid-cols-3">
        <Runtimes worker={worker} />
        <Scope worker={worker} />
        <Checkouts worker={worker} />
      </div>
      <Card data-testid="worker-heartbeat">
        <CardHeader className="sm:grid-cols-[1fr_auto]">
          <CardTitle>
            <h2>{t("heartbeat.title")}</h2>
          </CardTitle>
          <p className="text-sm text-muted-foreground sm:col-start-2 sm:row-start-1 sm:self-center" data-testid="worker-last-heartbeat">
            {worker.last_heartbeat_at ? (
              <>
                {t("heartbeat.last")} <Ago value={worker.last_heartbeat_at} never="" />
              </>
            ) : (
              t("heartbeat.never")
            )}
          </p>
        </CardHeader>
        <CardContent>
          <HeartbeatStrip workerId={worker.id} createdAt={worker.created_at} />
        </CardContent>
      </Card>
      <Card data-testid="worker-runs">
        <CardHeader>
          <CardTitle>
            <h2>{t("runs.title")}</h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          <p className="tabular-nums">
            {t("runs.held", { held: worker.held_runs, free: Math.max(worker.slots - worker.held_runs, 0) })}
          </p>
          <WorkerRuns worker={worker} />
        </CardContent>
      </Card>
    </div>
  );
}

function Note({ tone, icon: Icon, children, testId }: { tone: "info" | "warning" | "muted"; icon: LucideIcon; children: ReactNode; testId?: string }) {
  return (
    <p
      className={cn(
        "flex items-start gap-2.5 rounded-lg border px-3 py-2.5 text-sm",
        tone === "info" && "border-info-foreground/15 bg-info text-info-foreground",
        tone === "warning" && "border-warning-foreground/20 bg-warning text-warning-foreground",
        tone === "muted" && "bg-card text-muted-foreground",
      )}
      data-testid={testId}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span className="text-pretty">{children}</span>
    </p>
  );
}

function StatusNote({ worker, isOwner, admin }: { worker: Worker; isOwner: boolean; admin: boolean }) {
  const t = useTranslations("workers.detail");
  const format = useFormatter();
  const when = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium", timeStyle: "short" });
  const view = workerView(worker);
  const notes: ReactNode[] = [];
  if (view === "revoked" && worker.revoked_at) {
    notes.push(
      <Note key="revoked" tone="muted" icon={Ban} testId="worker-revoked">
        {t("revoked", { time: when(worker.revoked_at) })}
      </Note>,
    );
  } else {
    if (worker.drained_at) {
      notes.push(
        <Note key="draining" tone="warning" icon={Pause} testId="worker-draining">
          {isOwner
            ? t("draining", { time: when(worker.drained_at) })
            : t("drainingNotOwner", { time: when(worker.drained_at), owner: worker.owner })}
        </Note>,
      );
    }
    if (view === "offline") {
      notes.push(
        <Note key="offline" tone="muted" icon={WifiOff} testId="worker-offline">
          {worker.last_heartbeat_at
            ? t("offline", { time: when(worker.last_heartbeat_at) })
            : t.rich("neverSeen", {
                hostname: worker.hostname,
                code: (chunks) => <code className="rounded bg-muted px-1 font-mono text-xs text-foreground">{chunks}</code>,
              })}
        </Note>,
      );
    }
    if (admin && !isOwner) {
      notes.push(
        <Note key="admin" tone="info" icon={ShieldAlert} testId="worker-as-admin">
          {t("asAdmin", { owner: worker.owner })}
        </Note>,
      );
    }
    notes.push(
      <Note key="help" tone="muted" icon={Info}>
        {t("actionsHelp")}
      </Note>,
    );
  }
  return <div className="flex flex-col gap-2.5">{notes}</div>;
}

function RuntimeIcon({ runtime }: { runtime: RuntimeInfo }) {
  if (runtime.available === true) return <CircleCheck className="mt-0.5 size-4 shrink-0 text-chart-3" aria-hidden="true" />;
  if (runtime.available === false) return <CircleMinus className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />;
  return <CircleHelp className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />;
}

function SectionCard({ title, description, children, testId }: { title: string; description: string; children: ReactNode; testId: string }) {
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

function Runtimes({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.detail.runtimes");
  const runtimes = readRuntimes(worker.runtimes);
  return (
    <SectionCard title={t("title")} description={t("description")} testId="worker-runtimes">
      {runtimes.length === 0 ? (
        <p className="text-sm text-muted-foreground">{worker.last_heartbeat_at ? t("none") : t("empty")}</p>
      ) : (
        <ul className="flex flex-col divide-y">
          {runtimes.map((runtime) => (
            <li key={runtime.key} className="flex items-start gap-2.5 py-2 first:pt-0 last:pb-0" data-runtime={runtime.key}>
              <RuntimeIcon runtime={runtime} />
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="text-sm font-medium">{runtime.name}</span>
                {runtime.version || runtime.detail ? (
                  <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                    {[runtime.version ? t("version", { version: runtime.version }) : null, runtime.detail].filter(Boolean).join(", ")}
                  </span>
                ) : null}
              </div>
              <Badge variant={runtime.available === true ? "success" : runtime.available === false ? "outline" : "secondary"}>
                {runtime.available === true ? t("available") : runtime.available === false ? t("missing") : t("unknown")}
              </Badge>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

function Scope({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.detail.scope");
  return (
    <SectionCard title={t("title")} description={t("description")} testId="worker-scope">
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2.5 text-sm">
        <dt className="text-muted-foreground">{t("projects")}</dt>
        <dd className="min-w-0">
          <ChipList items={worker.projects} empty="-" />
        </dd>
        <dt className="text-muted-foreground">{t("slots")}</dt>
        <dd className="tabular-nums">{t("slotsValue", { held: worker.held_runs, slots: worker.slots })}</dd>
        <dt className="text-muted-foreground">{t("labels")}</dt>
        <dd className="min-w-0">
          <ChipList items={worker.labels} empty={t("noLabels")} />
        </dd>
        <dt className="text-muted-foreground">{t("permissions")}</dt>
        <dd className="flex items-start gap-1.5 text-warning-foreground">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          <span>{t("permissionsValue", { owner: worker.owner })}</span>
        </dd>
        <dt className="text-muted-foreground">{t("dispatch")}</dt>
        <dd>{t("dispatchValue", { owner: worker.owner })}</dd>
        <dt className="text-muted-foreground">{t("terminal")}</dt>
        <dd>{worker.allow_web_terminal ? t("terminalOn") : t("terminalOff")}</dd>
      </dl>
    </SectionCard>
  );
}

function Checkouts({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.detail.checkouts");
  const checkouts = readCheckouts(worker.checkouts);
  return (
    <SectionCard title={t("title")} description={t("description")} testId="worker-checkouts">
      {checkouts.length === 0 ? (
        <p className="text-sm text-muted-foreground">{worker.last_heartbeat_at ? t("none") : t("empty")}</p>
      ) : (
        <ul className="flex flex-col divide-y">
          {checkouts.map((checkout) => (
            <li key={checkout.name} className="flex items-start gap-2.5 py-2 first:pt-0 last:pb-0" data-checkout={checkout.name}>
              <GitBranch className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className="font-mono text-sm font-medium [overflow-wrap:anywhere]">{checkout.name}</span>
                {checkout.path || checkout.branch || checkout.detail ? (
                  <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
                    {[checkout.path, checkout.branch, checkout.detail].filter(Boolean).join(", ")}
                  </span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

const ACTION_ICONS = { drain: Pause, undrain: Play, revoke: Power } as const;

function WorkerActions({ worker, isOwner, onNotice }: { worker: Worker; isOwner: boolean; onNotice: (notice: Notice) => void }) {
  const t = useTranslations("workers.actions");
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, action: WorkerAction) => changeWorker(api, worker.id, action));
  const [confirming, setConfirming] = useState<"drain" | "revoke" | null>(null);
  const view = workerView(worker);
  if (view === "revoked") return null;
  const drained = worker.drained_at !== null;

  const run = async (action: WorkerAction) => {
    try {
      await write.mutateAsync(action); // resolves once the worker queries were reloaded
    } catch (error) {
      const failed = failure(error);
      if (action === "undrain" || failed.status === 404 || failed.status === 409) {
        setConfirming(null); // the page shows what the hub holds now; the reason goes above it
        onNotice({ tone: "error", text: failed.text, detail: failed.detail, requestId: failed.requestId });
      }
      return; // anything else stays in the dialog
    }
    setConfirming(null);
    onNotice({ tone: "success", text: t(`${action}Success`, { name: worker.name }) });
  };

  const open = (action: "drain" | "revoke") => {
    write.reset();
    setConfirming(action);
  };
  const error = write.isError && confirming ? failure(write.error) : null;
  const pendingUndrain = write.isPending && write.variables === "undrain";

  return (
    <>
      <div className="flex flex-wrap gap-2" data-testid="worker-actions">
        {drained ? (
          isOwner ? (
            <Button
              type="button"
              variant="outline"
              size="lg"
              onClick={() => void run("undrain")}
              aria-disabled={write.isPending || undefined}
              data-testid="worker-undrain"
            >
              {pendingUndrain ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Play aria-hidden="true" />}
              {pendingUndrain ? t("undrainPending") : t("undrain")}
            </Button>
          ) : null
        ) : (
          <Button type="button" variant="outline" size="lg" onClick={() => open("drain")} data-testid="worker-drain">
            <Pause aria-hidden="true" />
            {t("drain")}
          </Button>
        )}
        <Button
          type="button"
          variant="outline"
          size="lg"
          className="text-destructive hover:text-destructive"
          onClick={() => open("revoke")}
          data-testid="worker-revoke"
        >
          <Power aria-hidden="true" />
          {t("revoke")}
        </Button>
      </div>
      <ConfirmByName
        open={confirming !== null}
        onOpenChange={(next) => {
          if (!next) setConfirming(null);
        }}
        name={worker.name}
        icon={ACTION_ICONS[confirming ?? "drain"]}
        tone={confirming === "revoke" ? "danger" : "warning"}
        title={confirming === "revoke" ? t("revokeTitle", { name: worker.name }) : t("drainTitle", { name: worker.name })}
        description={
          confirming === "revoke" ? (
            <>
              <p>{t("revokeBody")}</p>
              {worker.held_runs > 0 ? <p className="font-medium text-foreground">{t("revokeHeld", { count: worker.held_runs })}</p> : null}
              <p>{t("audit")}</p>
            </>
          ) : (
            <>
              <p>{t("drainBody")}</p>
              {!isOwner ? <p className="font-medium text-foreground">{t("drainNotOwner", { owner: worker.owner })}</p> : null}
            </>
          )
        }
        confirmLabel={confirming === "revoke" ? t("revokeConfirm") : t("drainConfirm")}
        pendingLabel={confirming === "revoke" ? t("revokePending") : t("drainPending")}
        pending={write.isPending}
        error={error}
        onConfirm={() => void run(confirming ?? "drain")}
        testId={confirming === "revoke" ? "revoke-worker-dialog" : "drain-worker-dialog"}
      />
    </>
  );
}
