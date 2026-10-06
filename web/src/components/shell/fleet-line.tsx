"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { SidebarMenu, SidebarMenuButton, SidebarMenuItem } from "@/components/ui/sidebar";
import { WORKERS_HREF } from "@/components/workers/queries";
import { cn } from "@/lib/utils";

import { type Fleet, useFleet } from "./nav-counts";

type Tone = "success" | "danger" | "neutral";

const DOT: Record<Tone, string> = {
  success: "bg-success-solid",
  danger: "bg-danger-solid",
  neutral: "bg-neutral-solid",
};

/** The tone of the fleet: some worker online (success), every one offline or draining (danger), none yet (neutral). */
export function fleetTone(fleet: Fleet): Tone {
  if (fleet.online > 0) return "success";
  return fleet.registered > 0 ? "danger" : "neutral";
}

/**
 * The fleet line at the foot of the sidebar: how many of the visitor's workers are online and how many hold a run
 * ("2 workers online, 1 busy"), from the workers list the Workers page reads (GET /v1/workers, every 10 seconds). A
 * link to that page; folded, the dot alone with the words in a tooltip. Its height is kept while the list loads.
 */
export function FleetLine({ onNavigate }: { onNavigate?: () => void }) {
  const t = useTranslations("nav.fleet");
  const state = useFleet();

  if (state.status === "loading") {
    return (
      <div
        aria-hidden="true"
        data-testid="fleet-line-loading"
        className="h-8 shrink-0 rounded-sm bg-muted max-md:h-11 group-data-[collapsible=icon]:size-8"
      />
    );
  }

  const bold = (chunks: ReactNode) => <b className="font-medium text-foreground">{chunks}</b>;
  let tone: Tone = "neutral";
  let words: ReactNode = t("error");
  if (state.status === "ready") {
    const { fleet } = state;
    tone = fleetTone(fleet);
    if (fleet.online > 0 && fleet.busy > 0) words = t.rich("onlineBusy", { online: fleet.online, busy: fleet.busy, b: bold });
    else if (fleet.online > 0) words = t.rich("onlineIdle", { online: fleet.online, b: bold });
    else if (fleet.registered > 0) words = t.rich("offline", { registered: fleet.registered, b: bold });
    else words = t("none");
  }
  const fleet = state.status === "ready" ? state.fleet : null;

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <SidebarMenuButton
          asChild
          tooltip={{ children: words }}
          className="gap-2 bg-muted text-xs leading-4 font-normal hover:bg-accent hover:text-muted-foreground group-data-[collapsible=icon]:justify-center"
        >
          <Link
            href={WORKERS_HREF}
            onClick={onNavigate}
            data-testid="fleet-line"
            data-tone={tone}
            data-online={fleet?.online}
            data-busy={fleet?.busy}
            data-registered={fleet?.registered}
          >
            <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", DOT[tone])} />
            <span className="min-w-0 flex-1 truncate group-data-[collapsible=icon]:sr-only">{words}</span>
          </Link>
        </SidebarMenuButton>
      </SidebarMenuItem>
    </SidebarMenu>
  );
}
