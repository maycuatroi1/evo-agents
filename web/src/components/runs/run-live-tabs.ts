"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";

import { HELD_STATES, type Run } from "./queries";
import type { RunViewer } from "./run-model";

/** A live tab of a run's page (the Terminal, the Browser) as the page offers it. */
export type LiveTab = { open: boolean; sessionCreatedAt: string | null };

/**
 * Whether the visitor may watch the run's browser: its dispatcher, on a worker run started with a browser. `open` says
 * whether a worker holds the run now, so its browser is up; null for anyone else, whom the hub refuses (4403). It lives
 * here, apart from the browser's wire model, so the run page loads that model only with the Browser tab.
 */
export function browserAccess(
  run: Pick<Run, "dispatched_by" | "browser" | "kind" | "state" | "worker_id">,
  viewer: Pick<RunViewer, "login"> | null,
): { open: boolean } | null {
  if (!viewer || viewer.login !== run.dispatched_by || !run.browser || run.kind === "local") return null;
  return { open: (HELD_STATES as readonly string[]).includes(run.state) && run.worker_id !== null };
}

/**
 * A live tab from what the visitor may reach (`access`, null for a visitor the hub would refuse): once offered, the tab
 * stays while the page is open, so a session that ended with the run still shows why. It carries when the visitor's
 * web session was created, which the hub refuses past 12 hours.
 */
export function useLiveTab(access: { open: boolean } | null): LiveTab | null {
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const [offered, setOffered] = useState(false);
  if (access?.open && !offered) setOffered(true);
  if (access === null || !(access.open || offered)) return null;
  return { open: access.open, sessionCreatedAt: me?.token.kind === "web" ? me.token.created_at : null };
}
