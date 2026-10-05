/**
 * A Content-Security-Policy header read as directives and their sources, so a spec checks a source as a whole token.
 * A substring check cannot tell 'unsafe-eval' (any JavaScript eval) from 'wasm-unsafe-eval' (WebAssembly only,
 * which the run page's terminal needs).
 */
export function cspDirectives(policy: string): Map<string, string[]> {
  const directives = new Map<string, string[]>();
  for (const part of policy.split(";")) {
    const [name, ...sources] = part.trim().split(/\s+/);
    if (name) directives.set(name.toLowerCase(), sources);
  }
  return directives;
}

/** Every source of every directive of `policy`. */
export function cspSources(policy: string): string[] {
  return [...cspDirectives(policy).values()].flat();
}
