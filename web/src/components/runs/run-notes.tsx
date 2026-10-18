"use client";

import { Ban, Hand, HeartPulse, Info, type LucideIcon, ShieldAlert, Undo2 } from "lucide-react";
import dynamic from "next/dynamic";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import { HELD_STATES, type Run, STEADY_GAP_SECONDS, STEADY_SECONDS } from "./queries";
import { attachCommand } from "./run-actions";
import type { RunControls } from "./run-model";

/** The triage of a failed run is loaded when its page shows one: most runs never fail. */
const RunTriage = dynamic(() => import("./run-triage").then((module) => module.RunTriage), { ssr: false });

/** The notes over a run's trace (run-detail.tsx): what the run waits for, one line each. */

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
      <span className="min-w-0 text-pretty">{children}</span>
    </p>
  );
}

/**
 * What the run waits for, above the stepper: the owner's open asks (cancel, takeover, handback) until the worker
 * answers them, how to reach the session while the owner holds it, and, for anyone else, whose run it is. For anyone,
 * a queued attempt after a run lost on a worker that is not steady yet: which worker it waits for, from when that
 * worker takes it if its heartbeats keep coming, and whether another worker may take it meanwhile.
 */
export function RunNotes({ run, controls }: { run: Run; controls: RunControls }) {
  const t = useTranslations("runs.detail.notes");
  const format = useFormatter();
  const when = (value: string) => format.dateTime(new Date(value), { timeStyle: "medium" });
  const notes: ReactNode[] = [];
  const held = (HELD_STATES as readonly string[]).includes(run.state);
  if (held && run.cancel_requested_at) {
    notes.push(
      <Note key="cancel" tone="warning" icon={Ban} testId="run-note-cancel">
        {t("cancelAsked", { time: when(run.cancel_requested_at) })}
      </Note>,
    );
  }
  if (controls.takeover === "asked" && run.takeover_requested_at) {
    notes.push(
      <Note key="takeover" tone="info" icon={Hand} testId="run-note-takeover">
        {t("takeoverAsked", { time: when(run.takeover_requested_at), worker: run.worker ?? "-" })}
      </Note>,
    );
  }
  if (controls.handback === "asked" && run.handback_requested_at) {
    notes.push(
      <Note key="handback" tone="info" icon={Undo2} testId="run-note-handback">
        {t("handbackAsked", { time: when(run.handback_requested_at) })}
      </Note>,
    );
  }
  if (controls.owner && run.state === "interactive") {
    notes.push(
      <Note key="interactive" tone="warning" icon={Hand} testId="run-note-interactive">
        {t.rich("interactive", {
          worker: run.worker ?? "-",
          command: attachCommand(run.id),
          code: (chunks) => <code className="font-mono font-medium">{chunks}</code>,
        })}
      </Note>,
    );
  }
  const wait = run.state === "queued" ? run.steady_wait : null;
  if (wait) {
    const values = { attempt: run.attempt, worker: wait.worker, parent: run.parent_run_id ?? "-", seconds: STEADY_SECONDS, gap: STEADY_GAP_SECONDS };
    notes.push(
      <Note key="steady" tone="info" icon={HeartPulse} testId="run-note-steady">
        {wait.steady_at ? t("steadyWait", { ...values, time: when(wait.steady_at) }) : t("steadyWaitSilent", values)}{" "}
        {run.pinned_worker_id === wait.worker_id ? t("steadyPinned", { worker: wait.worker }) : t("steadyOthers")}
      </Note>,
    );
  }
  if (!controls.owner && !["done", "failed", "lost", "cancelled"].includes(run.state)) {
    notes.push(
      <Note key="owner" tone="muted" icon={ShieldAlert} testId="run-note-owner">
        {t("notOwner", { login: run.dispatched_by })}
      </Note>,
    );
  }
  if (controls.owner && run.state === "review") {
    notes.push(
      <Note key="review" tone="info" icon={Info} testId="run-note-review">
        {controls.approve ? t("review") : t("reviewNoWriter")}
      </Note>,
    );
  }
  return notes.length ? <div className="flex flex-col gap-2.5">{notes}</div> : null;
}

/** What goes over a run's trace: its notes, then the triage of a run that failed (run-triage.tsx). */
export function RunNotices({ run, controls }: { run: Run; controls: RunControls }) {
  return (
    <>
      <RunNotes run={run} controls={controls} />
      <RunTriage run={run} owner={controls.owner} />
    </>
  );
}
