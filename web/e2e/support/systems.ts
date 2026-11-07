import { call } from "../../src/lib/api/client";

import type { Account } from "./hub";
import { apiOf } from "./memories";

/**
 * A project's agent systems through the real API, as `evo-agents hub system put|show` does: the specs seed a system
 * this way, move it on behind the designer's back, and read back what the designer saved.
 */
export type SpecNode = { id: string; kind: string; profile?: string; version?: number; graph?: string; label?: string; position: { x: number; y: number } };
export type SpecEdge = { from: string; to: string; kind: string; description?: string };
export type SystemSpec = { schema: "evo-agent-system/1"; name: string; purpose?: string; nodes: SpecNode[]; edges: SpecEdge[] };

export function systemsPath(project: string): string {
  return `/p/${project}/systems`;
}

export function systemPath(project: string, slug: string): string {
  return `${systemsPath(project)}/${slug}`;
}

/** The entry, one agent per handle (the first is the entry agent) and the first one talking to each of the others. */
export function chainSpec(name: string, agents: { handle: string; version: number }[]): SystemSpec {
  const nodes: SpecNode[] = [{ id: "entry", kind: "entry", position: { x: 0, y: 0 } }];
  const edges: SpecEdge[] = [];
  agents.forEach((agent, index) => {
    nodes.push({ id: agent.handle, kind: "agent", profile: agent.handle, version: agent.version, position: { x: 300, y: index * 140 } });
    edges.push(index === 0 ? { from: "entry", to: agent.handle, kind: "interacts" } : { from: agents[0].handle, to: agent.handle, kind: "interacts" });
  });
  return { schema: "evo-agent-system/1", name, nodes, edges };
}

/** Write `spec` as system `slug` of `project`: a new system when `ifRevision` is absent, else its next revision. */
export async function putSystemByApi(admin: Account, project: string, slug: string, spec: SystemSpec, ifRevision?: number) {
  const api = await apiOf(admin);
  return call(
    api.PUT("/v1/projects/{project}/systems/{slug}", {
      params: { path: { project, slug } },
      body: { spec, ...(ifRevision ? { if_revision: ifRevision } : {}) },
    }),
  );
}

export async function systemOf(account: Account, project: string, slug: string) {
  const api = await apiOf(account);
  return call(api.GET("/v1/projects/{project}/systems/{slug}", { params: { path: { project, slug } } }));
}

/** A new version of agent `handle`, as an edit on its page makes one. */
export async function bumpAgentByApi(admin: Account, project: string, handle: string, description: string) {
  const api = await apiOf(admin);
  return call(api.PATCH("/v1/projects/{project}/agents/{handle}", { params: { path: { project, handle } }, body: { description } }));
}
