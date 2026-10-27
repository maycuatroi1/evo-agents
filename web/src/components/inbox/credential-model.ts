import type { Decision } from "./queries";

/**
 * The pure parts of a credential request (`evo_agents.hub.credential_requests`, docs/credentials.md "A run that asks
 * for a credential"): a decision of category credential that asks the run's owner for a variable's value, their hub
 * account, or both. The hub gives it its own options, whose keys these are.
 */

export type CredentialRequest = NonNullable<Decision["credential_request"]>;

/** The value pasted: the hub keeps it as the owner's secret of kind env for the run's project. */
export const VALUE = "value";
/** The owner's account on the hub: the run calls the hub's API as them, secrets, tokens and workers aside. */
export const AS_ME = "as_me";
/** Both of them. */
export const BOTH = "both";
/** Nothing: the agent goes on without it. */
export const REFUSE = "refuse";

/** `evo_agents.hub.credentials.MAX_SECRET_BYTES`: one secret's value, as UTF-8. */
export const MAX_SECRET_BYTES = 16 * 1024;

/** What the decision asks for, or null for a decision that is no credential request. */
export function credentialRequest(decision: Partial<Pick<Decision, "category" | "credential_request">>): CredentialRequest | null {
  return decision.category === "credential" ? (decision.credential_request ?? null) : null;
}

/** Whether choosing `option` gives the variable's value, which the answer then carries. */
export function givesValue(option: string | null): boolean {
  return option === VALUE || option === BOTH;
}

/** Whether choosing `option` lets the run call the hub as its owner. */
export function grantsAccount(option: string | null): boolean {
  return option === AS_ME || option === BOTH;
}

/**
 * The option picked when the form opens on a credential request the agent recommends nothing for: the value alone when
 * the request asks for a variable, the narrowest answer that gives the run what it needs, so the owner pastes and
 * sends; nothing when it asks only for the account, which the owner picks on purpose. Null for a decision that is no
 * credential request, or one whose options lack the value.
 */
export function credentialOption(decision: Pick<Decision, "options"> & Partial<Pick<Decision, "category" | "credential_request">>): string | null {
  const request = credentialRequest(decision);
  if (!request?.env_var) return null;
  return decision.options.some((option) => option.key === VALUE) ? VALUE : null;
}

export type ValueProblem = "missing" | "long" | "nul" | "inWords" | null;

/**
 * What keeps a value from going, as the hub checks it: none pasted for an option that gives it, more than
 * MAX_SECRET_BYTES of UTF-8, a NUL character, or the value inside the owner's words, which go to the agent.
 */
export function valueProblem(option: string | null, value: string, words: string): ValueProblem {
  if (!givesValue(option)) return null;
  if (value === "") return "missing";
  if (new TextEncoder().encode(value).length > MAX_SECRET_BYTES) return "long";
  if (value.includes("\u0000")) return "nul";
  if (words.includes(value)) return "inWords";
  return null;
}

/** The words of the send button for `option`: what the answer gives; null for the form's usual Send answer. */
export function sendWords(option: string | null): "sendValue" | "sendAccount" | "sendBoth" | "sendRefuse" | null {
  if (option === VALUE) return "sendValue";
  if (option === AS_ME) return "sendAccount";
  if (option === BOTH) return "sendBoth";
  if (option === REFUSE) return "sendRefuse";
  return null;
}

/** The hint under the form for `option`: where the value goes, that the run acts as the owner, or both. */
export function footWords(option: string | null): "footValue" | "footAccount" | "footBoth" | null {
  if (option === BOTH) return "footBoth";
  if (option === VALUE) return "footValue";
  if (option === AS_ME) return "footAccount";
  return null;
}
