import { useTranslations } from "next-intl";

/**
 * The runtimes of the core (`runs.RUNTIMES`), which the messages name. A run's runtime is any (until a worker claims
 * it), one of these, or a plugin's `<plugin>/<name>` (`evo_agents.plugins.runtimes`), which the pages show as it is.
 */
export const CORE_RUNTIMES = ["claude-code", "opencode", "codex"] as const;
export type CoreRuntime = (typeof CORE_RUNTIMES)[number];

/** What the dispatch forms offer: any, or a runtime of the core; a plugin's is dispatched by name (`hub run dispatch`). */
export type RequestedRuntime = "any" | CoreRuntime;

export function isCoreRuntime(name: string): name is CoreRuntime {
  return (CORE_RUNTIMES as readonly string[]).includes(name);
}

/** A runtime as the pages name it: the message of any and of each runtime of the core, a plugin's name as it is. */
export function useRuntimeName(): (runtime: string) => string {
  const t = useTranslations("runs.runtime");
  return (runtime) => (runtime === "any" || isCoreRuntime(runtime) ? t(runtime) : runtime);
}
