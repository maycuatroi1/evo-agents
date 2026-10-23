"use client";

import { ArrowLeft, ArrowRight, Hand, LogIn, RotateCw, TextCursorInput, TriangleAlert, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";

import { notifyFailure } from "@/components/feedback/toast";
import { useStatusText } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useMediaQuery, TOUCH_QUERY } from "@/hooks/use-media-query";
import { useIsMobile } from "@/hooks/use-mobile";
import { useNow } from "@/hooks/use-now";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { LOGIN_PATH } from "@/lib/config";
import { cn } from "@/lib/utils";

import { BrowserScreen, type ScreenHandle } from "./browser-screen";
import { type BrowserEvent, type BrowserTab, openUrl, tabName } from "./browser-view-model";
import { useRunControl, useRunFailure } from "./hooks";
import type { Run } from "./queries";
import type { RunControls } from "./run-model";
import { type CloseKind, sessionHours, sessionTooOld } from "./terminal-model";
import { useRunBrowser } from "./use-run-browser";

/**
 * The Browser tab of a run started with a browser, for its dispatcher: the run's Chromium on the worker, live. The
 * agent drives it and the person watches and picks the tab watched; Take control (the run's takeover) gives it to the
 * person once the agent's turn ends, and Hand back returns it. The view connects while the tab is shown and the page
 * is in sight, and says something of the connection only when it is in trouble. Save and Done, which later steps add
 * (a login session's, a login decision's), sit beside Take control.
 */

/** A page out of sight this long (ms) lets the view go; it connects again when the page is back. */
const AWAY_MS = 30_000;
/** Closes that are the hub or the network failing, said as errors; the rest are the view ending. */
const ERROR_KINDS: ReadonlySet<CloseKind> = new Set(["lost", "unavailable", "protocol", "forbidden", "upgrade", "signIn", "shutdown"]);

type Message = { tone: "info" | "error"; text: string; detail?: string; action?: ReactNode };

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

/** Whether the page has been out of sight for AWAY_MS. */
function usePageAway(): boolean {
  const [away, setAway] = useState(false);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onChange = () => {
      clearTimeout(timer);
      if (document.visibilityState === "hidden") timer = setTimeout(() => setAway(true), AWAY_MS);
      else setAway(false);
    };
    document.addEventListener("visibilitychange", onChange);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onChange);
    };
  }, []);
  return away;
}

function TabStrip({ tabs, active, label, onPick }: { tabs: BrowserTab[]; active: string | null; label: string; onPick: (id: string) => void }) {
  return (
    <nav aria-label={label} className="border-b">
      <ul className="flex gap-1 overflow-x-auto px-3 py-1.5" data-testid="browser-tabs">
        {tabs.map((tab) => {
          const current = tab.id === active;
          return (
            <li key={tab.id} className="shrink-0">
              <button
                type="button"
                aria-current={current ? "true" : undefined}
                title={tab.url}
                onClick={() => !current && onPick(tab.id)}
                className={cn(
                  "flex h-8 max-w-52 cursor-pointer items-center rounded-sm border px-2.5 text-[13px] transition-colors max-md:h-11",
                  current ? "border-border-strong bg-surface-selected font-medium text-foreground" : "border-transparent text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
                data-testid="browser-tab"
                data-tab-id={tab.id}
              >
                <span className="truncate">{tabName(tab)}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

export function RunBrowserPanel({
  run,
  open,
  active,
  sessionCreatedAt,
  controls,
  save = null,
  done = null,
  fill = false,
}: {
  run: Pick<Run, "id" | "project" | "plan_id" | "state" | "worker">;
  /** A worker holds the run, so its browser is up. */
  open: boolean;
  /** The Browser tab is the one shown. */
  active: boolean;
  /** When the visitor's web session was created (whoami), for the 12-hour limit. */
  sessionCreatedAt: string | null;
  controls: Pick<RunControls, "takeover" | "handback">;
  /** A login session's Save. */
  save?: ReactNode;
  /** A login decision's Done. */
  done?: ReactNode;
  /** From the xl breakpoint ("always": at every width), the screen takes the height the parent gives. */
  fill?: boolean | "always";
}) {
  const t = useTranslations("runs.detail.browser");
  const tActions = useTranslations("runs.detail.actions");
  const tState = useStatusText("run");
  const ids = useId();
  const worker = run.worker ?? "";
  const now = useNow(active);
  const tooOld = sessionTooOld(sessionCreatedAt, now);
  const away = usePageAway();
  const touch = useMediaQuery(TOUCH_QUERY);
  const phone = useIsMobile();
  const screen = useRef<ScreenHandle>(null);
  const [sawFrame, setSawFrame] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const [leaving, setLeaving] = useState(false);

  const view = useRunBrowser({
    project: run.project,
    runId: run.id,
    enabled: active && open && !tooOld && !away,
    onScreen: (header, jpeg) => screen.current?.draw(header, jpeg) ?? Promise.resolve(),
    sessionOld: () => sessionTooOld(sessionCreatedAt, Date.now()),
  });
  const { status, end, worker: linked, state, notice, retrying, send, reconnect } = view;
  const human = state?.mode === "human";
  const drives = status === "live" && linked && human;
  const tab = state?.tabs.find((entry) => entry.id === state.active) ?? null;

  const control = useRunControl(run);
  const failure = useRunFailure();
  const act = (action: "takeover" | "handback") =>
    control.mutate(action, { onError: (error) => notifyFailure(tActions(`toast.failed.${action}`, { id: run.id }), failure(error)) });
  const pending = control.isPending ? control.variables : null;

  const onFrame = useCallback(() => setSawFrame(true), []);
  const go = (event: BrowserEvent) => void send(event);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const url = openUrl(draft ?? "");
    if (!url || !drives) return;
    go({ type: "open", url });
    setDraft(null);
    screen.current?.focusKeys();
  };

  const signInButton = (
    <Button
      type="button"
      size="sm"
      onClick={() => {
        setLeaving(true);
        void signInAgain();
      }}
      disabled={leaving}
      data-testid="browser-sign-in"
    >
      <LogIn aria-hidden="true" />
      {t("signIn")}
    </Button>
  );
  const again = (label: string) => (
    <Button type="button" size="sm" variant="outline" onClick={reconnect} data-testid="browser-reconnect">
      {label}
    </Button>
  );

  // Only trouble is said; the quiet states (connecting, waiting for the worker) are the placeholder in the screen.
  const message = ((): Message | null => {
    if (tooOld) return { tone: "error", text: t("tooOld", { hours: sessionCreatedAt && now !== null ? sessionHours(sessionCreatedAt, now) : 12 }), action: signInButton };
    if (!open) return null;
    if (status === "closed" && end) {
      const kind = end.kind;
      return {
        tone: ERROR_KINDS.has(kind) ? "error" : "info",
        text: t(`end.${kind}`, { worker }),
        detail: end.reason ? t("hubSays", { reason: end.reason }) : undefined,
        action: kind === "signIn" ? signInButton : kind === "busy" ? again(t("viewHere")) : kind === "forbidden" ? undefined : again(t("reconnect")),
      };
    }
    if (retrying) return { tone: "error", text: t("retrying") };
    if (status === "live" && !linked && sawFrame) return { tone: "info", text: t("workerGone", { worker }) };
    if (notice) return { tone: "info", text: t("notice", { notice }) };
    return null;
  })();

  let placeholder: string | null = null;
  if (!open) placeholder = t("notOpen", { id: run.id, state: tState(run.state) });
  else if (!sawFrame && !tooOld && status !== "closed") placeholder = status === "live" ? t("waiting", { worker }) : t("connecting");

  let take: ReactNode = null;
  if (controls.handback !== "none") {
    const busy = controls.handback === "asked" || pending === "handback";
    take = (
      <Button type="button" size="sm" onClick={() => act("handback")} busy={busy} data-testid="browser-handback">
        <Undo2 aria-hidden="true" />
        {busy ? t("handingBack") : t("handBack")}
      </Button>
    );
  } else if (controls.takeover !== "none") {
    const busy = controls.takeover === "asked" || pending === "takeover";
    take = (
      <Button type="button" size="sm" variant="outline" onClick={() => act("takeover")} busy={busy} data-testid="browser-takeover">
        <Hand aria-hidden="true" />
        {busy ? t("takingControl") : t("takeControl")}
      </Button>
    );
  }
  const asked = controls.takeover === "asked";

  const hint = !open ? null : human ? t("hint.human") : controls.takeover !== "none" ? t("hint.agent") : t("hint.watch");
  const boxFill = fill === "always" ? "h-auto min-h-0 flex-1" : fill ? "xl:h-auto xl:min-h-0 xl:flex-1" : undefined;

  return (
    <div
      className={cn("flex min-w-0 flex-col", fill === "always" ? "min-h-0 flex-1" : fill && "xl:min-h-0 xl:flex-1")}
      data-testid="browser-panel"
      data-status={status}
      data-mode={state?.mode}
      data-worker={linked || undefined}
      data-close-code={end?.code}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-2 border-b px-3 py-2">
        <div className="flex items-center">
          <Button type="button" variant="ghost" size="icon" aria-label={t("back")} disabled={!drives} onClick={() => go({ type: "back" })} data-testid="browser-back">
            <ArrowLeft aria-hidden="true" />
          </Button>
          <Button type="button" variant="ghost" size="icon" aria-label={t("forward")} disabled={!drives} onClick={() => go({ type: "forward" })} data-testid="browser-forward">
            <ArrowRight aria-hidden="true" />
          </Button>
          <Button type="button" variant="ghost" size="icon" aria-label={t("reload")} disabled={!drives} onClick={() => go({ type: "reload" })} data-testid="browser-reload">
            <RotateCw aria-hidden="true" />
          </Button>
        </div>
        <form onSubmit={submit} className="min-w-0 flex-1 basis-48" role="search" aria-label={t("address")}>
          <Input
            value={draft ?? tab?.url ?? ""}
            onChange={(event) => setDraft(event.target.value)}
            onFocus={(event) => {
              if (drives) {
                setDraft(event.target.value);
                event.target.select();
              }
            }}
            onBlur={() => setDraft(null)}
            onKeyDown={(event) => {
              if (event.key === "Escape") event.currentTarget.blur();
            }}
            readOnly={!drives}
            aria-label={t("address")}
            inputMode="url"
            enterKeyHint="go"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            autoComplete="off"
            className="font-mono text-[13px] read-only:bg-muted read-only:text-muted-foreground max-md:text-base"
            data-testid="browser-url"
          />
        </form>
        {take || save || done ? (
          <div className="flex items-center gap-2 max-sm:w-full max-sm:justify-end">
            {take}
            {save}
            {done}
          </div>
        ) : null}
      </div>
      {state && state.tabs.length > 1 ? <TabStrip tabs={state.tabs} active={state.active} label={t("tabs")} onPick={(id) => go({ type: "tab", id })} /> : null}
      {message ? (
        <div
          role={message.tone === "error" ? "alert" : "status"}
          className={cn("flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-2.5 text-sm", message.tone === "error" ? "bg-card text-danger" : "bg-accent text-accent-foreground")}
          data-testid="browser-message"
          data-kind={end?.kind ?? (retrying ? "retrying" : notice ? "notice" : undefined)}
        >
          {message.tone === "error" ? <TriangleAlert className="size-4 shrink-0" aria-hidden="true" /> : null}
          <div className="flex min-w-0 flex-1 basis-48 flex-col gap-0.5">
            <p className="text-pretty">{message.text}</p>
            {message.detail ? <p className="text-xs break-words opacity-90">{message.detail}</p> : null}
          </div>
          {message.action}
        </div>
      ) : null}
      <div className={cn("relative h-[min(60vh,32rem)] min-h-64 bg-term-bg", boxFill)}>
        <BrowserScreen
          ref={screen}
          human={drives}
          send={send}
          label={tab ? t("labelTab", { id: run.id, title: tabName(tab) }) : t("label", { id: run.id })}
          keysLabel={t("keys")}
          describedBy={`${ids}-hint`}
          onFrame={onFrame}
        >
          {placeholder ? (
            <p className="absolute inset-0 flex items-center justify-center px-6 text-center text-sm text-term-muted" data-testid="browser-placeholder">
              {placeholder}
            </p>
          ) : null}
        </BrowserScreen>
        {status === "closed" || !open ? <div className="pointer-events-none absolute inset-0 bg-term-bg/40" aria-hidden="true" /> : null}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-2 text-xs text-muted-foreground">
        <span id={`${ids}-hint`} data-testid="browser-hint">
          {asked && !human ? t("asked") : hint}
        </span>
        {drives && (touch || phone) ? (
          <Button type="button" size="sm" variant="outline" onClick={() => screen.current?.focusKeys()} data-testid="browser-keyboard">
            <TextCursorInput aria-hidden="true" />
            {t("keyboard")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}
