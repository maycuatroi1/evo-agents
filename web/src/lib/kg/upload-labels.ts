import type { Project } from "@/lib/api/client";

/**
 * The labels an upload may carry. In a project: the levels of its ladder up to what its hub sink clears, since the
 * hub refuses a label above that before any byte is sent (`ProjectRules.check_push`). In a new project: the default
 * ladder (`evo_agents.kg.policy.DEFAULT_LEVELS`), whose hub sink the hub sets to clear the level picked
 * (`kg_upload_project`), `internal` by default.
 */
export const NEW_PROJECT_LEVELS = ["public", "internal", "customer", "secret"] as const;
export const NEW_PROJECT_LEVEL = "internal";

export type UploadLevels =
  | { kind: "levels"; levels: string[]; preset: string }
  | { kind: "no-hub-sink" }
  | { kind: "not-writer" };

const WRITERS = new Set(["writer", "admin"]);

function defaultLevel(project: Project): string | null {
  const level = (project.default_label as { level?: unknown } | null)?.level;
  return typeof level === "string" ? level : null;
}

export function uploadLevels(project: Project): UploadLevels {
  if (!project.role || !WRITERS.has(project.role)) return { kind: "not-writer" };
  const hub = project.sinks.find((sink) => sink.kind === "hub");
  const top = hub ? project.levels.indexOf(hub.clearance.level) : -1;
  if (top < 0) return { kind: "no-hub-sink" };
  const levels = project.levels.slice(0, top + 1);
  const preferred = defaultLevel(project);
  const preset = preferred && levels.includes(preferred) ? preferred : levels[levels.length - 1];
  return { kind: "levels", levels, preset };
}
