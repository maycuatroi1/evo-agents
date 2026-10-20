"use client";

import { Bot, LoaderCircle, RotateCw, TriangleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type KeyboardEvent, useRef, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { notifyFailure } from "@/components/feedback/toast";
import { useSelectedProject } from "@/components/shell/project-switcher";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useDirectChat } from "@/hooks/use-direct-chat";
import type { Agent } from "@/lib/agents";
import { cn } from "@/lib/utils";

import { AgentStripSkeleton, STRIP_ROW } from "./agent-strip-skeleton";
import { useChatFailure } from "./hooks";
import { useAddableAgents } from "./use-addable-agents";

/** The key that moves the focus from the agent at `index` of `count`, or null for a key the row leaves alone. */
export function nextIndex(key: string, index: number, count: number): number | null {
  if (key === "ArrowRight") return (index + 1) % count;
  if (key === "ArrowLeft") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

/**
 * The row of agents at the head of the chat list (web/DESIGN.md, Chats): the agents the visitor may chat with (not
 * deleted, of the projects they write to, the project the shell shows first), each its avatar with its name under it
 * (two lines at most) and a tooltip with its name, @handle and project. Pressing one, or Enter on it, opens the visitor's direct chat with
 * that agent (the one there is, or a new one) with the composer focused; the agent of the direct chat shown is marked.
 * One Tab stop: the arrows, Home and End move along the row, which scrolls sideways on its own. While the agents load it
 * shows rounds in their place; it says so when there are none, and which projects' agents could not be read.
 */
export function AgentStrip({ current }: { current: string | null }) {
  const t = useTranslations("chats.strip");
  const format = useFormatter();
  const selected = useSelectedProject();
  const addable = useAddableAgents(selected);
  const failure = useChatFailure();
  const direct = useDirectChat({ onError: (error, handle) => notifyFailure(t("openFailed", { handle }), failure(error)) });
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const [focus, setFocus] = useState(0);

  if (addable.status === "loading") return <AgentStripSkeleton />;

  const { agents, failed } = addable;
  const problem = failed.length ? (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 pb-2 text-xs text-danger" data-testid="agent-strip-failed">
      <TriangleAlert className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1">{t("failed", { projects: format.list(failed, { type: "conjunction" }) })}</span>
      <Button type="button" variant="ghost" size="sm" onClick={addable.retry} data-testid="agent-strip-retry">
        <RotateCw aria-hidden="true" />
        {t("retry")}
      </Button>
    </p>
  ) : null;

  if (agents.length === 0) {
    return (
      <div className="border-b" data-testid="agent-strip" data-state={failed.length ? "error" : "empty"}>
        {failed.length ? (
          <div className="pt-2">{problem}</div>
        ) : (
          <p className="flex items-start gap-2 px-4 py-2.5 text-xs text-pretty text-muted-foreground" data-testid="agent-strip-empty">
            <Bot className="mt-px size-3.5 shrink-0" aria-hidden="true" />
            {t("empty")}
          </p>
        )}
      </div>
    );
  }

  const at = Math.min(focus, agents.length - 1);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const next = nextIndex(event.key, at, agents.length);
    if (next === null) return;
    event.preventDefault();
    setFocus(next);
    buttons.current[next]?.focus();
  };

  return (
    <div className="border-b" data-testid="agent-strip" data-state="ready">
      <div role="toolbar" aria-label={t("label")} aria-orientation="horizontal" onKeyDown={onKeyDown} className={STRIP_ROW} data-testid="agent-strip-agents">
        {agents.map((agent, index) => (
          <StripAgent
            key={`${agent.project}/${agent.handle}`}
            agent={agent}
            ref={(node) => {
              buttons.current[index] = node;
            }}
            tabbable={index === at}
            current={agent.handle === current}
            opening={direct.opening === agent.handle}
            onFocus={() => setFocus(index)}
            onOpen={() => direct.open(agent.handle)}
          />
        ))}
      </div>
      {problem}
    </div>
  );
}

function StripAgent({
  agent,
  ref,
  tabbable,
  current,
  opening,
  onFocus,
  onOpen,
}: {
  agent: Agent;
  ref: (node: HTMLButtonElement | null) => void;
  tabbable: boolean;
  current: boolean;
  opening: boolean;
  onFocus: () => void;
  onOpen: () => void;
}) {
  const t = useTranslations("chats.strip");
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          ref={ref}
          type="button"
          tabIndex={tabbable ? 0 : -1}
          aria-label={t("open", { name: agent.name, handle: agent.handle, project: agent.project })}
          aria-current={current ? "true" : undefined}
          aria-busy={opening || undefined}
          onFocus={onFocus}
          onClick={onOpen}
          className="group/agent flex w-18 shrink-0 cursor-pointer snap-start flex-col items-center gap-1 rounded-md px-1 py-1.5 transition-colors hover:bg-accent aria-busy:cursor-progress"
          data-testid="agent-strip-agent"
          data-handle={agent.handle}
          data-project={agent.project}
        >
          <span className={cn("relative rounded-full", current && "ring-2 ring-brand ring-offset-2 ring-offset-card")}>
            <AgentAvatar agent={agent} size="lg" />
            {opening ? (
              <span className="absolute inset-0 grid place-items-center rounded-full bg-background/70" data-testid="agent-strip-opening">
                <LoaderCircle className="size-5 animate-spin text-running motion-reduce:animate-none" aria-hidden="true" />
              </span>
            ) : null}
          </span>
          <span
            className={cn(
              "line-clamp-2 w-full text-center text-[11px] leading-[14px] [overflow-wrap:anywhere]",
              current ? "font-medium text-foreground" : "text-muted-foreground group-hover/agent:text-foreground",
            )}
          >
            {agent.name}
          </span>
        </button>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="flex-col items-start gap-0.5">
        <span className="font-medium">{agent.name}</span>
        <span className="opacity-80">{t("tooltip", { handle: agent.handle, project: agent.project })}</span>
      </TooltipContent>
    </Tooltip>
  );
}
