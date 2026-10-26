"use client";

import { useTranslations } from "next-intl";

import type { ChatProgress } from "@/lib/chats";
import { cn } from "@/lib/utils";

/** The steps a person waits through: no agent works on the message yet, so their dot is neutral and still. */
const WAITING = new Set<ChatProgress["step"]>(["waiting", "inbox"]);

/**
 * How far each agent's run that took a message is (web/DESIGN.md, Chats; docs/hub.md, Group chats): one line per run,
 * under the message, while the run is on its way, said by the hub by rule from the run's state and its worker's notes.
 * The first says the agent got the message and what it waits for (the worker, or the turn that runs); each step of
 * the run's setup replaces it (claimed, checking access, opening a repo, starting the agent), then working. The line
 * goes once the agent answers or the run ends, so nothing shows for a message nobody works on. A waiting line carries
 * a still neutral dot, a step of work the pulsing running one. Each line is a polite status: a reader of the screen
 * hears each step once, whole.
 */
export function MessageProgress({ progress }: { progress: readonly ChatProgress[] | undefined }) {
  const t = useTranslations("chats.message.progress");
  if (!progress?.length) return null;
  return (
    <div className="flex flex-col gap-0.5" data-testid="message-progress">
      {progress.map((item) => {
        const waiting = WAITING.has(item.step);
        const values = { handle: item.handle, worker: item.worker ?? "", repo: item.repo ?? "" };
        const key = item.step === "waiting" && !item.worker ? "waitingAny" : item.step;
        return (
          <p
            key={item.run_id}
            role="status"
            className="flex min-w-0 items-start gap-2 text-xs leading-5 text-muted-foreground"
            data-testid="message-progress-step"
            data-run-id={item.run_id}
            data-step={item.step}
          >
            <span className={cn("relative mt-1.5 inline-flex size-2 shrink-0 rounded-full", waiting ? "bg-neutral-solid" : "bg-running")} aria-hidden="true">
              {waiting ? null : <span className="absolute inset-0 animate-live-ping rounded-full bg-running" />}
            </span>
            <span className="min-w-0 [overflow-wrap:anywhere]">
              {t.rich(key, { ...values, agent: (chunks) => <span className="rounded-xs bg-brand/10 px-0.5 font-medium text-brand">{chunks}</span> })}
            </span>
          </p>
        );
      })}
    </div>
  );
}
