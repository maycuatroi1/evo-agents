import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import { CLAIMS, type ClaimKey } from "./claims";
import { docHref } from "./links";

/** A link inside running text: underlined as well as coloured, so it never rests on colour alone (WCAG 1.4.1). */
export const TEXT_LINK = "text-brand underline underline-offset-4 hover:text-brand-hover";

/**
 * A claim of the product page about evo-agents: its sentences from `product.claims.<id>`, each tag a link to the
 * public docs section that describes the behaviour (CLAIMS).
 */
export function Claim({ id, className }: { id: ClaimKey; className?: string }) {
  const t = useTranslations("product.claims");
  const tags = Object.fromEntries(
    Object.entries(CLAIMS[id]).map(([tag, doc]) => [
      tag,
      (chunks: ReactNode) => (
        <a href={docHref(doc)} className={TEXT_LINK}>
          {chunks}
        </a>
      ),
    ]),
  );
  return (
    <p className={cn("text-sm text-pretty", className)} data-claim={id}>
      {t.rich(id, tags)}
    </p>
  );
}
