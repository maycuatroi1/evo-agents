"use client";

import type { TerminalHandle } from "@wterm/react";
import { Hourglass, Loader2, LogIn, type LucideIcon, Radio, SquareTerminal, TriangleAlert, Unplug } from "lucide-react";
import dynamic from "next/dynamic";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";

import { useNow } from "@/components/kg/use-now";
import { useStatusText } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { LOGIN_PATH } from "@/lib/config";
import { cn } from "@/lib/utils";

import type { Run } from "./queries";
import { type CloseKind, sessionHours, sessionTooOld, TERMINAL_OPEN_STATES } from "./terminal-model";
import { type TerminalStatus, useRunTerminal } from "./use-run-terminal";

const TerminalView = dynamic(() => import("./terminal-view"), {
  ssr: false,
  loading: () => <div className="hub-terminal" aria-busy="true" data-testid="terminal-loading" />,
});

/**
 * The Terminal tab of a run's page, for the run's owner on a worker of theirs that allows it (`terminalAccess`):
 * Connect opens the agent's TUI, which runs in tmux on the worker, through the hub. On a headless run the hub asks for
 * a takeover first, so the agent stops at the end of its turn and its session resumes in the terminal. The hub decides
 * again when the socket opens and says why when it closes it; a sign-in older than 12 hours asks to sign in again
 * before anything is tried.
 */

type Shown = TerminalStatus | "loading";

const STATUS_LOOK: Record<Shown, { icon: LucideIcon; variant: "secondary" | "info" | "warning" | "destructive"; spin?: boolean }> = {
  idle: { icon: Unplug, variant: "secondary" },
  loading: { icon: Loader2, variant: "secondary", spin: true },
  connecting: { icon: Loader2, variant: "secondary", spin: true },
  waiting: { icon: Hourglass, variant: "warning" },
  live: { icon: Radio, variant: "info" },
  closed: { icon: Unplug, variant: "secondary" },
};

type Message = { tone: "info" | "error"; text: string; detail?: string };

/** Closes that are the hub or the network failing, shown as errors; the rest are the session ending. */
const ERROR_KINDS: ReadonlySet<CloseKind> = new Set(["lost", "unavailable", "protocol", "forbidden", "busy", "upgrade", "signIn"]);

/** Revoke the old web session, then sign in again from the sign-in page (best effort: the page asks either way). */
async function signInAgain(): Promise<void> {
  try {
    const api = browserApi();
    const { csrf, header } = await call(api.GET("/v1/auth/web/csrf"));
    await call(api.POST("/v1/auth/web/logout", { headers: { [header]: csrf } }));
  } catch {
    // expired or revoked already: the sign-in page takes it from there
  }
  window.location.assign(LOGIN_PATH);
}

export function RunTerminalPanel({
  run,
  open,
  active,
  sessionCreatedAt,
}: {
  run: Pick<Run, "id" | "project" | "state" | "mode" | "worker">;
  /** The run's state lets a terminal open now. */
  open: boolean;
  /** The Terminal tab is the one shown. */
  active: boolean;
  /** When the visitor's web session was created (whoami), for the 12-hour limit. */
  sessionCreatedAt: string | null;
}) {
  const t = useTranslations("runs.detail.terminal");
  const tState = useStatusText("run");
  const format = useFormatter();
  const ids = useId();
  const worker = run.worker ?? "";
  const now = useNow(active);
  const tooOld = sessionTooOld(sessionCreatedAt, now);
  const handle = useRef<TerminalHandle>(null);
  const [session, setSession] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [leaving, setLeaving] = useState(false);
  const [sawOutput, setSawOutput] = useState(false);

  const terminal = useRunTerminal({
    project: run.project,
    runId: run.id,
    onOutput: (bytes) => handle.current?.write(bytes),
    sessionOld: () => sessionTooOld(sessionCreatedAt, Date.now()),
  });
  const { status, end, size, connect, disconnect, send, resize } = terminal;
  const shown: Shown = session > 0 && status === "idle" && !loadError ? "loading" : status;
  const busy = shown === "loading" || shown === "connecting" || shown === "waiting" || shown === "live";
  // A run whose agent runs headless now is taken over when the terminal opens; an interactive one is attached to.
  const takeover = run.state !== "interactive" && run.mode === "headless";
  if (status === "live" && !sawOutput) setSawOutput(true);
  // After a session that printed something, its last screen stays (read-only); one refused before that shows the intro.
  const showScreen = session > 0 && !loadError && (shown !== "closed" || sawOutput);

  // Once the agent's TUI answers, keys go to it.
  useEffect(() => {
    if (status === "live" && active) handle.current?.focus();
  }, [status, active]);

  const start = () => {
    setLoadError(null);
    setSawOutput(false);
    setSession((value) => value + 1);
  };
  const onReady = useCallback((cols: number, rows: number) => connect(cols, rows), [connect]);
  const onData = useCallback((data: string) => void send(data), [send]);
  const onBinary = useCallback((data: Uint8Array) => void send(data), [send]);
  const onError = useCallback((error: unknown) => {
    setLoadError(error instanceof Error ? error.message : String(error));
  }, []);

  const look = STATUS_LOOK[shown];
  const failed = shown === "closed" && end !== null && ERROR_KINDS.has(end.kind);
  const StatusIcon = failed ? TriangleAlert : look.icon;
  const signInNeeded = tooOld || (shown === "closed" && end?.kind === "signIn");
  const hours = sessionCreatedAt && now !== null ? sessionHours(sessionCreatedAt, now) : 12;

  const message = ((): Message | null => {
    if (loadError) return { tone: "error", text: t("loadFailed"), detail: loadError };
    if (tooOld && !busy) return { tone: "error", text: t("tooOld", { hours }) };
    if (shown === "waiting") {
      return { tone: "info", text: takeover ? t("waiting.takeover", { worker }) : t("waiting.interactive", { worker }) };
    }
    if (shown === "closed" && end) {
      return {
        tone: ERROR_KINDS.has(end.kind) ? "error" : "info",
        text: t(`end.${end.kind}`, { worker }),
        detail: end.reason ? t("hubSays", { reason: end.reason }) : undefined,
      };
    }
    if (!open && !busy) {
      const states = format.list(
        TERMINAL_OPEN_STATES.map((state) => tState(state)),
        { type: "disjunction" },
      );
      return { tone: "info", text: t("notOpen", { id: run.id, state: tState(run.state), open: states }) };
    }
    return null;
  })();

  let action: ReactNode;
  if (signInNeeded) {
    action = (
      <Button
        type="button"
        onClick={() => {
          setLeaving(true);
          void signInAgain();
        }}
        disabled={leaving}
        data-testid="terminal-sign-in"
      >
        <LogIn aria-hidden="true" />
        {t("signIn")}
      </Button>
    );
  } else if (busy) {
    action = (
      <Button type="button" variant="outline" onClick={disconnect} disabled={shown === "loading"} data-testid="terminal-disconnect">
        <Unplug aria-hidden="true" />
        {t("disconnect")}
      </Button>
    );
  } else {
    action = (
      <Button type="button" onClick={start} disabled={!open} data-testid="terminal-connect">
        <SquareTerminal aria-hidden="true" />
        {session > 0 ? t("connectAgain") : t("connect")}
      </Button>
    );
  }

  return (
    <div className="flex min-w-0 flex-col" data-testid="terminal-panel" data-status={shown} data-close-code={end?.code}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-3">
        <span role="status" className="inline-flex">
          <Badge variant={failed ? "destructive" : look.variant} data-testid="terminal-status" data-status={shown}>
            <StatusIcon className={cn(look.spin && !failed && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />
            {t(`status.${shown}`, { worker })}
          </Badge>
        </span>
        <p className="min-w-0 flex-1 basis-48 text-sm text-muted-foreground">{t("where", { worker })}</p>
        {action}
      </div>
      {message ? (
        <div
          role={message.tone === "error" ? "alert" : "status"}
          className={cn(
            "flex items-start gap-2.5 border-b px-4 py-2.5 text-sm",
            message.tone === "error" ? "bg-card text-danger" : "bg-accent text-accent-foreground",
          )}
          data-testid="terminal-message"
          data-kind={end?.kind}
        >
          {message.tone === "error" ? (
            <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          ) : (
            <Hourglass className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          )}
          <div className="flex min-w-0 flex-col gap-0.5">
            <p className="text-pretty">{message.text}</p>
            {message.detail ? <p className="text-xs break-words opacity-90">{message.detail}</p> : null}
          </div>
        </div>
      ) : null}
      <div className="h-[min(60vh,32rem)] min-h-64 bg-term-bg">
        {showScreen ? (
          <div className={cn("h-full", shown === "closed" && "opacity-70")} data-testid="terminal-view">
            <TerminalView
              key={session}
              handle={handle}
              label={t("label", { id: run.id, worker })}
              describedBy={`${ids}-hint`}
              paused={!active}
              onReady={onReady}
              onData={onData}
              onBinary={onBinary}
              onResize={resize}
              onError={onError}
            />
          </div>
        ) : (
          <div className="flex h-full flex-col gap-3 overflow-y-auto px-4 py-3 font-mono text-[12.5px] leading-[1.6] text-term-muted" data-testid="terminal-intro">
            <p className="text-term-fg">
              {takeover ? t("intro.headless", { id: run.id, worker }) : t("intro.interactive", { worker })}
            </p>
            <p>{t("intro.limits")}</p>
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-2 text-xs text-muted-foreground">
        <span id={`${ids}-hint`}>{t("hint", { worker })}</span>
        {size && shown !== "closed" ? (
          <span className="font-mono tabular-nums" aria-label={t("sizeLabel", size)} data-testid="terminal-size">
            {t("size", size)}
          </span>
        ) : null}
      </div>
    </div>
  );
}
