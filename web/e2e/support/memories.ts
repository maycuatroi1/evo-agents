import { type ApiClient, call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { type Account, bearerClient, machineToken, type Registration, registration } from "./hub";

/**
 * Memories seeded through the real API with a member's machine token, as `evo-agents hub memory push` writes them.
 * Writes go through the project's hub sink, which `memoryProject` clears up to secret, so a writer reads back what
 * it wrote at any level.
 */
export type Memory = components["schemas"]["Memory"];
export type MemoryType = "project" | "reference" | "user" | "feedback";

export type MemoryInput = {
  name: string;
  body: string;
  project?: string;
  location?: string;
  type?: MemoryType;
  level?: string;
  ifRevision?: number;
};

/** A project whose hub sink clears every level, with two repos: memories at any label can be pushed to it. */
export function memoryProject(): Registration {
  return registration({
    sinks: [
      { id: "hub", kind: "hub", clearance: { level: "secret" } },
      { id: "claude-code@anthropic", kind: "agent-session", clearance: { level: "internal" } },
    ],
    repos: [
      { name: "api", origin: "https://github.com/example-org/api", default_branch: "main", path: "api" },
      { name: "web", origin: "https://github.com/example-org/web", default_branch: "main", path: "web" },
    ],
  });
}

export async function apiOf(account: Account): Promise<ApiClient> {
  return bearerClient(await machineToken(account));
}

export async function putMemory(api: ApiClient, input: MemoryInput): Promise<Memory> {
  const personal = input.project === undefined;
  return call(
    api.PUT("/v1/memories", {
      params: { query: { sink: "hub" } },
      body: {
        scope: personal ? "personal" : "project",
        project: input.project ?? null,
        location: input.location ?? (personal ? "notes-e2e" : "harness"),
        name: input.name,
        type: input.type ?? "project",
        body: input.body,
        label: personal ? null : { level: input.level ?? "internal", integrity: "U" },
        if_revision: input.ifRevision ?? null,
      },
    }),
  );
}

/** A memory file as Claude Code writes one: frontmatter with name, description and type, then the text. */
export function memoryFile(title: string, description: string, text: string, type: MemoryType = "project"): string {
  return `---\nname: ${title}\ndescription: ${description}\nmetadata:\n  type: ${type}\n---\n${text}\n`;
}
