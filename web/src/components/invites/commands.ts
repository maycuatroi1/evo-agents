import { PROJECT_NAME } from "@/lib/queries";

/**
 * The commands of the Get started list, as README.md documents them: the CLI with the knowledge graph builder and the
 * worker, its sign-in to this hub, the evo-hub and evo-kg plugins, a harness and its registration for a register link,
 * and the worker service. A test checks each one against README.md.
 */

export const INSTALL_COMMAND = "uv tool install 'evo-ak[graphify,worker]'";

/** `components/workers/model.ts` SERVICE_COMMAND: the worker daemon kept running in the background. */
export const SERVICE_COMMAND = "evo-agents worker service install";

export const REGISTER_COMMAND = "evo-agents hub project register";

const MARKETPLACE = "https://github.com/maycuatroi1/evo-agents";

export const CLAUDE_PLUGIN_COMMANDS = [
  `claude plugin marketplace add ${MARKETPLACE}`,
  "claude plugin install evo-hub@evo-agents",
  "claude plugin install evo-kg@evo-agents",
] as const;

/** Codex takes the evo-hub plugin (README.md, Codex). */
export const CODEX_PLUGIN_COMMANDS = [`codex plugin marketplace add ${MARKETPLACE}`, "codex plugin add evo-hub@evo-agents"] as const;

export function hubLoginCommand(hubUrl: string): string {
  return `evo-agents hub login --url ${hubUrl}`;
}

/** A harness for the repos of the current directory; without a valid name the project takes the directory's. */
export function harnessInitCommand(project: string): string {
  const name = project.trim();
  return PROJECT_NAME.test(name) ? `evo-agents harness init --project ${name}` : "evo-agents harness init";
}
