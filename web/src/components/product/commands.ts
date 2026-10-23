/**
 * The commands of the product page's Get started, as README.md documents them: the CLI with the knowledge graph
 * builder and the worker, its sign-in to this hub, and the evo-hub and evo-kg plugins for Claude Code. commands.test.ts
 * checks each one against README.md.
 */

export const INSTALL_COMMAND = "uv tool install 'evo-ak[graphify,worker]'";

export const PLUGIN_COMMANDS = [
  "claude plugin marketplace add https://github.com/maycuatroi1/evo-agents",
  "claude plugin install evo-hub@evo-agents",
  "claude plugin install evo-kg@evo-agents",
] as const;

export function hubLoginCommand(hubUrl: string): string {
  return `evo-agents hub login --url ${hubUrl}`;
}
