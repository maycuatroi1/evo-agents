/** The two first steps shared with README.md; full onboarding lives in docs/getting-started.md. */
export const INSTALL_COMMAND = "uv tool install 'evo-ak[graphify,worker]'";

export function hubLoginCommand(hubUrl: string): string {
  return `evo-agents hub login --url ${hubUrl}`;
}
