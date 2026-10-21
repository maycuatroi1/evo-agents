"use client";

import { useQueries } from "@tanstack/react-query";

import { useWriterProjects } from "@/hooks/use-direct-chat";
import { agentsQuery } from "@/lib/agent-queries";
import { type Agent, usableAgents } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";

export type AddableAgents =
  | { status: "loading" }
  | { status: "ready"; agents: Agent[]; failed: string[]; retry: () => void };

/** `projects` in name order, `first` (when among them) ahead of the others. */
export function projectOrder(projects: readonly string[], first: string | null = null): string[] {
  const sorted = [...projects].sort((a, b) => a.localeCompare(b));
  return first && sorted.includes(first) ? [first, ...sorted.filter((project) => project !== first)] : sorted;
}

/**
 * The agents the visitor may add to a chat or chat with: those not deleted of each project they write to
 * (`chat_members`: adding an agent of project P needs the writer role on P), the projects in name order (`first`, the
 * project the shell shows, ahead) and each one's default agent first. A project whose agents could not be read is
 * named in `failed`, so the form can say so, and `retry` reads them again.
 */
export function useAddableAgents(first: string | null = null): AddableAgents {
  const projects = useWriterProjects();
  const ordered = projectOrder(projects ?? [], first);
  const reads = useQueries({ queries: ordered.map((project) => agentsQuery(browserApi, project)) });
  if (projects === null || reads.some((read) => read.isPending)) return { status: "loading" };
  const agents = reads.flatMap((read) => usableAgents(read.data ?? []));
  const failed = ordered.filter((_, index) => reads[index].isError);
  const retry = () => {
    for (const read of reads) if (read.isError) void read.refetch();
  };
  return { status: "ready", agents, failed, retry };
}
