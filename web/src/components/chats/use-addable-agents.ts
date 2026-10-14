"use client";

import { useQueries } from "@tanstack/react-query";

import { agentsQuery } from "@/lib/agent-queries";
import { type Agent, usableAgents } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";

import { useWriterProjects } from "./hooks";

export type AddableAgents = { status: "loading" } | { status: "ready"; agents: Agent[]; failed: string[] };

/**
 * The agents the visitor may add to a chat: those not deleted of each project they write to (`chat_members`: adding
 * an agent of project P needs the writer role on P), the projects in name order and each one's default agent first.
 * A project whose agents could not be read is named in `failed`, so the form can say so.
 */
export function useAddableAgents(): AddableAgents {
  const projects = useWriterProjects();
  const sorted = [...(projects ?? [])].sort((a, b) => a.localeCompare(b));
  const reads = useQueries({ queries: sorted.map((project) => agentsQuery(browserApi, project)) });
  if (projects === null || reads.some((read) => read.isPending)) return { status: "loading" };
  const agents = reads.flatMap((read) => usableAgents(read.data ?? []));
  const failed = sorted.filter((_, index) => reads[index].isError);
  return { status: "ready", agents, failed };
}
