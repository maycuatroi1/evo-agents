"use client";

import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useStatusText } from "@/components/status/status-badge";

import type { RunMove } from "./log-model";
import { CHAT_TAB, decisionAnchor, RUN_VIEW_PARAM } from "./queries";
import { SESSION_TABS, type SessionTab } from "./run-log";

/** The state of a run's page that is not the run's: the session's tab, the phone's trace screen, and how moves read. */

/** A move between states as the log says it: "Running to Verifying, by the worker: ..." */
export function useDescribeMove() {
  const t = useTranslations("runs.detail.log");
  const tState = useStatusText("run");
  return useCallback(
    (move: RunMove) => {
      const to = tState(move.to);
      const actor = move.actor === "worker" || move.actor === "owner" || move.actor === "reaper" ? t(`actor.${move.actor}`) : move.actor;
      const from = move.from ? tState(move.from) : null;
      const text = from ? (actor ? t("moveBy", { from, to, actor }) : t("move", { from, to })) : t("moveTo", { to });
      return move.reason ? t("moveReason", { move: text, reason: move.reason }) : text;
    },
    [t, tState],
  );
}

/**
 * The tab the session card shows, held by the page and kept in the URL (`?view=log`, `?view=outputs`, `?view=terminal`; the first tab,
 * the Trace or an author run's Chat, has none): the Terminal tab once offered when the page was opened on it (a
 * decision's Take over from the Inbox), or when a decision's Take over here asks for it. The notice author_waiting
 * links to `?tab=chat`, the Chat, which an author run's page shows first anyway. Changing tabs replaces the URL without a
 * navigation.
 */
export function useSessionTab(terminalOffered: boolean, first: SessionTab) {
  const params = useSearchParams();
  const asked = params.get(RUN_VIEW_PARAM) ?? (params.get("tab") === CHAT_TAB ? CHAT_TAB : null);
  const wanted: SessionTab | null = (SESSION_TABS as readonly string[]).includes(asked ?? "") ? (asked as SessionTab) : null;
  const [tab, setTab] = useState<SessionTab>(wanted === "log" || wanted === "trace" || wanted === "outputs" || (wanted === "chat" && first === "chat") ? wanted : first);
  const [applied, setApplied] = useState(false);
  if (wanted === "terminal" && terminalOffered && !applied) {
    setApplied(true);
    setTab("terminal");
  }
  const choose = useCallback(
    (next: SessionTab) => {
      setTab(next);
      const url = new URL(window.location.href);
      url.searchParams.delete("tab");
      if (next === first) url.searchParams.delete(RUN_VIEW_PARAM);
      else url.searchParams.set(RUN_VIEW_PARAM, next);
      window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
    },
    [first],
  );
  return [tab, choose] as const;
}

/**
 * A phone's trace screen: the session on a screen of its own, an entry of the browser's history so Back closes it.
 * Opening shows the tab asked for and scrolls to the top; a page opened on another tab than the first (`?view=log`,
 * Take over's Terminal) is on the screen too, which the page decides from the tab.
 */
export function useTraceScreen(setTab: (tab: SessionTab) => void, first: SessionTab) {
  const [screen, setScreen] = useState(false);
  // Leaving the screen goes back to the session's first tab: a phone's page shows the trace's preview, and a tab other
  // than the first (the Raw log, the Terminal) is a screen of its own.
  useEffect(() => {
    const onPop = () => {
      const open = Boolean((window.history.state as { runScreen?: boolean } | null)?.runScreen);
      setScreen(open);
      if (!open) setTab(first);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [setTab, first]);
  const open = (tab: SessionTab) => {
    setTab(tab);
    setScreen(true);
    window.history.pushState({ ...window.history.state, runScreen: true }, "", window.location.href);
    window.scrollTo(0, 0);
  };
  const close = () => {
    if ((window.history.state as { runScreen?: boolean } | null)?.runScreen) window.history.back();
    else {
      setScreen(false);
      setTab(first);
    }
  };
  return [screen, open, close] as const;
}

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Shows a tab of the session card: scrolls the card (the element `cardId`) into view and focuses the tab; on a phone,
 * opens the session's screen on it.
 */
export function useShowSession(cardId: string, mobile: boolean, setTab: (tab: SessionTab) => void, openScreen: (tab: SessionTab) => void) {
  return (tab: SessionTab) => {
    if (mobile) {
      openScreen(tab);
      return;
    }
    setTab(tab);
    const card = document.getElementById(cardId);
    card?.scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
    card?.querySelector<HTMLElement>(`[data-testid="run-tab-${tab}"]`)?.focus({ preventScroll: true });
  };
}

/** An author run's decisions are cards in its Chat: "Asked you" shows the tab, then the card, once it is drawn. */
export function useShowDecision(setTab: (tab: SessionTab) => void) {
  return useCallback(
    (id: number) => {
      setTab("chat");
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          const card = document.getElementById(decisionAnchor(id));
          card?.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
          card?.focus({ preventScroll: true });
        }),
      );
    },
    [setTab],
  );
}
