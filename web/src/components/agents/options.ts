import { queryOptions } from "@tanstack/react-query";

import { agentKeys } from "@/lib/agent-queries";
import { type Axis, LABEL_PREFIX } from "@/lib/agents";
import { AGENT_RUNTIMES, GITHUB_APP, HOST_ENVIRONMENT, TOOLS } from "@/lib/agent-rules";
import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the agent form offers on each axis, read from the hub as the admin sees it: their own secrets by name (and the
 * App's token), the global skills and the project's, the workers they see and those workers' labels, the models those
 * workers list, and the other projects of their grants; the tools, runtimes and host are the API's own words, and a
 * plugin's environment, `<plugin>/<name>`, is typed by hand. A list the hub
 * does not answer is left empty and named in `failed`; the form still takes a value typed by hand on every axis, since
 * an allowlist may name what the admin cannot see (another member's secret or worker).
 */
type Schemas = components["schemas"];
type Secret = Schemas["Secret"];
type Skill = Schemas["Skill"];
type Worker = Schemas["Worker"];
type Project = Schemas["Project"];

/** One choice of an axis: its value as the allowlist keeps it, a word for its kind, and a line about it. */
export type AxisOption = { value: string; tag?: string; detail?: string };
export type AgentOptions = { byAxis: Record<Axis, AxisOption[]>; failed: Axis[] };

type Sources = {
  secrets: Secret[] | null;
  skills: Skill[] | null;
  workers: Worker[] | null;
  projects: Project[] | null;
};

const byValue = (a: AxisOption, b: AxisOption) => a.value.localeCompare(b.value);

function runtimeModels(report: unknown): string[] {
  if (typeof report !== "object" || report === null || Array.isArray(report)) return [];
  const { available, models } = report as Record<string, unknown>;
  if (available === false || !Array.isArray(models)) return [];
  return models.filter((model): model is string => typeof model === "string" && model.trim() !== "");
}

/** The choices of each axis from what the hub answered; null for a list it did not answer. */
export function optionsFrom(sources: Sources, project: string): AgentOptions {
  const failed: Axis[] = [];
  if (sources.secrets === null) failed.push("credential");
  if (sources.skills === null) failed.push("skill");
  if (sources.workers === null) failed.push("worker", "model");
  if (sources.projects === null) failed.push("project");

  const credential: AxisOption[] = [
    { value: GITHUB_APP, tag: "githubApp" },
    ...(sources.secrets ?? []).map((secret) => ({ value: secret.name, tag: secret.kind, detail: secret.env_var ?? secret.url_prefix ?? undefined })).sort(byValue),
  ];

  const skills = new Map<string, AxisOption>();
  for (const skill of sources.skills ?? []) {
    if (!skills.has(skill.name)) skills.set(skill.name, { value: skill.name, tag: skill.scope, detail: skill.description || undefined });
  }

  const workers = (sources.workers ?? []).filter((worker) => worker.status !== "revoked");
  const labels = new Map<string, number>();
  const models = new Map<string, Set<string>>();
  for (const worker of workers) {
    for (const label of worker.labels) labels.set(label.toLowerCase(), (labels.get(label.toLowerCase()) ?? 0) + 1);
    for (const runtime of AGENT_RUNTIMES) {
      for (const model of runtimeModels(worker.runtimes[runtime])) models.set(model, (models.get(model) ?? new Set()).add(runtime));
    }
  }
  const worker: AxisOption[] = [
    ...workers.map((each) => ({ value: each.name.toLowerCase(), tag: "worker", detail: each.owner })).sort(byValue),
    ...[...labels].map(([label, count]) => ({ value: `${LABEL_PREFIX}${label}`, tag: "label", detail: String(count) })).sort(byValue),
  ];

  return {
    byAxis: {
      credential,
      tool: TOOLS.map((tool) => ({ value: tool, tag: tool })),
      skill: [...skills.values()].sort(byValue),
      worker: worker.filter((option, index) => worker.findIndex((other) => other.value === option.value) === index),
      project: (sources.projects ?? [])
        .filter((other) => other.name !== project)
        .map((other) => ({ value: other.name, detail: other.role ?? undefined }))
        .sort(byValue),
      model: [...models].map(([model, runtimes]) => ({ value: model, detail: [...runtimes].join(", ") })).sort(byValue),
      runtime: AGENT_RUNTIMES.map((runtime) => ({ value: runtime, tag: runtime })),
      environment: [{ value: HOST_ENVIRONMENT, tag: "host" }],
    },
    failed,
  };
}

async function settled<T>(pending: Promise<T>): Promise<T | null> {
  try {
    return await pending;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    return null;
  }
}

/** The form's choices, read when the form opens; each list on its own, so one the hub refuses leaves the rest. */
export const agentOptionsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: [...agentKeys.all(project), "options"] as const,
    queryFn: async ({ signal }) => {
      const [secrets, skills, workers, projects] = await Promise.all([
        settled(call(api().GET("/v1/secrets", { signal }))),
        settled(call(api().GET("/v1/skills", { params: { query: { project } }, signal }))),
        settled(call(api().GET("/v1/workers", { signal }))),
        settled(call(api().GET("/v1/projects", { signal }))),
      ]);
      return optionsFrom({ secrets, skills, workers, projects }, project);
    },
    staleTime: 30_000,
  });
