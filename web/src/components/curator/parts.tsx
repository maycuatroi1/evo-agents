/** A link inside a line of text: `brand` with a quiet underline, so colour is not all that sets it apart (axe). */
export const TEXT_LINK = "rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current";

/**
 * Small pieces the Curator's pages share: a list of facts (term over value on a phone, side by side from the sm
 * breakpoint) and money as the hub counts it, in US dollars. They live in components/data/facts.tsx, which the
 * Releases page shares too.
 */
export { Fact, Facts, useDay, useMoney } from "@/components/data/facts";
