"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Ban,
  CircleCheck,
  CircleHelp,
  CircleMinus,
  GitBranch,
  Info,
  type LucideIcon,
  Pause,
  Play,
  Power,
  ShieldAlert,
  TriangleAlert,
  WifiOff,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useId, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { WorkerRuns } from "@/components/runs/worker-runs";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { Ago } from "./ago";
import { ChipList } from "./badges";
import { ConfirmByName } from "./confirm-by-name";
import { HeartbeatStrip } from "./heartbeat-strip";
import { useRecordPrefetched, useWorkerFailure, useWorkerWrite } from "./hooks";
import { readCheckouts, readRuntimes, type RuntimeInfo, workerView } from "./model";
import {
  changeWorker,
  type DispatchFrom,
  setDispatchFrom,
  type Worker,
  type WorkerAction,
  workerKeys,
  workerQuery,
} from "./queries";

/**
 * One worker: its runtimes, scope and checkouts, the last hour of heartbeats, its latest runs, drain, resume and
 * revoke, and, for its owner, the switch that keeps it to runs dispatched from the web.
 */
export function WorkerDetail({ id, initialError }: { id: number; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("workers.detail");
  // The page's main query: the top bar says from it whether the page is current (every 10 seconds).
  const state = useHubQuery(workerQuery(browserApi, id), initialError, { live: true });
  useRecordPrefetched(workerKeys.one(id), (data) => [data as Worker]);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle")} description={t("notFoundDescription")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(worker) => <WorkerPage worker={worker} />}
    </QueryView>
  );
}

function WorkerPage({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.detail");
  const format = useFormatter();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const view = workerView(worker);
  const isOwner = me?.login === worker.owner;
  const registered = format.dateTime(new Date(worker.created_at), { dateStyle: "medium" });

  return (
    <div className="flex flex-col gap-6" data-testid="worker-detail" data-worker-id={worker.id}>
      <PageHeader
        title={worker.name}
        status={<StatusBadge kind="worker" status={view} size="lg" />}
        sub={t("facts", {
          os: worker.os,
          arch: worker.arch,
          hostname: worker.hostname,
          owner: worker.owner,
          version: worker.agent_version,
          date: registered,
        })}
        actions={<WorkerActions worker={worker} isOwner={isOwner} />}
      />
      <StatusNote worker={worker} isOwner={isOwner} admin={Boolean(me?.admin)} />
      <div className="grid gap-4 lg:grid-cols-3">
        <Runtimes worker={worker} />
        <Scope worker={worker} isOwner={isOwner} />
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
        "flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-sm",
        tone === "info" && "bg-muted text-foreground",
        tone === "warning" && "border-attention/20 bg-attention-soft text-attention",
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
  if (runtime.available === true) return <CircleCheck className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" />;
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

function Scope({ worker, isOwner }: { worker: Worker; isOwner: boolean }) {
  const t = useTranslations("workers.detail.scope");
  const webOnly = worker.dispatch_from === "web";
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
        <dd className="flex items-start gap-1.5 text-attention">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          <span>{t("permissionsValue", { owner: worker.owner })}</span>
        </dd>
        <dt className="text-muted-foreground">{t("dispatch")}</dt>
        <dd data-testid="worker-dispatch" data-dispatch-from={worker.dispatch_from}>
          {webOnly ? t("dispatchWeb", { owner: worker.owner }) : t("dispatchValue", { owner: worker.owner })}
        </dd>
        <dt className="text-muted-foreground">{t("terminal")}</dt>
        <dd>{worker.allow_web_terminal ? t("terminalOn") : t("terminalOff")}</dd>
      </dl>
      {isOwner && workerView(worker) !== "revoked" ? <DispatchFromSwitch worker={worker} /> : null}
    </SectionCard>
  );
}

/**
 * The owner's switch between runs dispatched with any of their credentials and runs dispatched from the web only. The
 * hub takes the change from a web session alone, and the switch shows what the hub holds: it moves once the hub
 * answers and the worker is read again. The result is a toast; a failure stays until dismissed.
 */
function DispatchFromSwitch({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.dispatchFrom");
  const id = useId();
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, value: DispatchFrom) => setDispatchFrom(api, worker.id, value));

  const change = async (webOnly: boolean) => {
    if (write.isPending) return;
    try {
      await write.mutateAsync(webOnly ? "web" : "any"); // resolves once the worker queries were reloaded
    } catch (error) {
      notifyFailure(t("failed", { name: worker.name }), failure(error));
      return;
    }
    notify(
      webOnly
        ? { tone: "success", text: t("webSuccess", { name: worker.name }), description: t("webSuccessText") }
        : { tone: "success", text: t("anySuccess", { name: worker.name }), description: t("anySuccessText") },
    );
  };

  return (
    <div className="mt-4 flex items-start gap-3 border-t pt-4" data-testid="worker-dispatch-from">
      <Switch
        id={`${id}-switch`}
        checked={worker.dispatch_from === "web"}
        onCheckedChange={(on) => void change(on)}
        aria-describedby={`${id}-hint`}
        aria-busy={write.isPending || undefined}
        className="mt-0.5"
        data-testid="worker-dispatch-from-switch"
      />
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor={`${id}-switch`} className="cursor-pointer text-sm font-medium">
          {t("label")}
        </label>
        <p id={`${id}-hint`} className="text-xs leading-4 text-pretty text-fg-subtle">
          {t("hint")}
        </p>
      </div>
    </div>
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

/**
 * Drain, Resume and Revoke. Each waits for the hub, then says what happened in a toast; a failure the confirm dialog can
 * not help with (Resume has none, a 404 or 409 means the worker changed) closes it and stays as a toast until dismissed.
 */
function WorkerActions({ worker, isOwner }: { worker: Worker; isOwner: boolean }) {
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
        setConfirming(null); // the page shows what the hub holds now; the reason stays in a toast
        notifyFailure(t(`${action}Failed`, { name: worker.name }), failed);
      }
      return; // anything else stays in the dialog
    }
    setConfirming(null);
    notify({ tone: "success", text: t(`${action}Success`, { name: worker.name }), description: t(`${action}SuccessText`) });
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
              onClick={() => void run("undrain")}
              aria-disabled={write.isPending || undefined}
              busy={pendingUndrain}
              data-testid="worker-undrain"
            >
              <Play aria-hidden="true" />
              {pendingUndrain ? t("undrainPending") : t("undrain")}
            </Button>
          ) : null
        ) : (
          <Button type="button" variant="outline" onClick={() => open("drain")} data-testid="worker-drain">
            <Pause aria-hidden="true" />
            {t("drain")}
          </Button>
        )}
        <Button
          type="button"
          variant="outline"
          className="text-danger hover:text-danger"
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
