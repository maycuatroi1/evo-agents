"use client";

import { Bot, MessageSquareReply } from "lucide-react";
import Link from "next/link";
import type { Route } from "next";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";

import type { Notification } from "./queries";

/**
 * A notification the Orchestrator of the project's default system holds (`ops` of the API; docs/hub.md, Agent
 * systems): queued in a batch that gathers or waits for its thread, or in that thread now, with a link to it. A row
 * without it waits on a person. The link sits over the row's stretched title, so both stay clickable.
 */
export function OpsTag({ notification }: { notification: Pick<Notification, "ops" | "title"> }) {
  const t = useTranslations("inbox.ops");
  const ops = notification.ops;
  if (!ops) return null;
  return (
    <span className="relative z-10 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs" data-testid="notification-ops" data-state={ops.state}>
      <Badge variant="outline" className="gap-1 text-muted-foreground">
        <Bot className="size-3" aria-hidden="true" />
        {ops.state === "running" ? t("handling") : t("queued")}
      </Badge>
      {ops.thread ? (
        <Link
          href={ops.thread as Route}
          className="inline-flex min-h-11 items-center gap-1 font-medium text-link underline-offset-4 hover:underline sm:min-h-0"
          aria-label={t("threadFor", { title: notification.title })}
          data-testid="notification-ops-thread"
        >
          <MessageSquareReply className="size-3.5" aria-hidden="true" />
          {t("thread")}
        </Link>
      ) : null}
    </span>
  );
}
