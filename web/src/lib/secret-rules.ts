/**
 * What an env secret may set, as the hub checks it (`credentials.ENV_NAME`, `DENIED_ENV`, `DENIED_ENV_PREFIXES` and
 * `env_name_refusal`, `secrets.MAX_ENV_VAR_CHARS`). A member's secret and an agent's account follow the same rule, so
 * the Secrets page and the accounts of an agent both read it from here; the hub decides again on every write.
 */
export const ENV_NAME = /^[A-Z_][A-Z0-9_]*$/;
export const DENIED_ENV = ["PATH", "HOME", "SHELL", "USER", "TMPDIR", "SSH_AUTH_SOCK"] as const;
export const DENIED_ENV_PREFIXES = ["EVO_", "GIT_", "LD_", "DYLD_", "PYTHON"] as const;
export const MAX_ENV_VAR_CHARS = 200;

/** Why `name` cannot be a secret's env_var, or null when it can (`credentials.env_name_refusal`). */
export function envVarProblem(name: string): "envVarRequired" | "envVarInvalid" | "envVarDenied" | null {
  if (!name) return "envVarRequired";
  if (name.length > MAX_ENV_VAR_CHARS || !ENV_NAME.test(name)) return "envVarInvalid";
  if ((DENIED_ENV as readonly string[]).includes(name) || DENIED_ENV_PREFIXES.some((prefix) => name.startsWith(prefix))) {
    return "envVarDenied";
  }
  return null;
}
