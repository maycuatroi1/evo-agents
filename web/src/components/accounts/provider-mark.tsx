import { cn } from "@/lib/utils";

import { inkOn } from "./provider-cards";
import { type Provider, ProviderLogo } from "./provider-logo";
import type { CatalogProvider } from "./provider-queries";

/** The providers drawn with their own logo; any other gets its catalog tile. */
const LOGOS: ReadonlySet<string> = new Set<Provider>(["claude", "anthropic"]);

/**
 * The mark of a provider of the catalog: its logo for the few the page draws (ProviderLogo), else its abbreviation
 * (`text_icon`) on its catalog colour, the ink white or black, whichever contrasts more. Decorative, as
 * the provider's name is text beside it; it says who the provider is, never how its keys are doing.
 */
export function ProviderMark({
  id,
  entry,
  size = "md",
  className,
}: {
  id: string;
  entry: Pick<CatalogProvider, "color" | "text_icon" | "name"> | null;
  size?: "sm" | "md";
  className?: string;
}) {
  const box = size === "md" ? "size-9 rounded-md" : "size-7 rounded-[6px]";
  if (LOGOS.has(id)) {
    return (
      <span aria-hidden="true" className={cn("inline-flex shrink-0 items-center justify-center border bg-card", box, className)}>
        <ProviderLogo provider={id as Provider} />
      </span>
    );
  }
  const color = entry?.color && /^#[0-9a-f]{6}$/i.test(entry.color) ? entry.color : null;
  const letters = (entry?.text_icon || entry?.name || id).slice(0, 2).toUpperCase();
  return (
    <span
      aria-hidden="true"
      data-provider-mark={id}
      className={cn(
        "inline-flex shrink-0 items-center justify-center font-semibold leading-none tracking-tight",
        box,
        size === "md" ? "text-[13px]" : "text-[11px]",
        color ? (inkOn(color) === "light" ? "text-white" : "text-black") : "bg-muted text-foreground",
        className,
      )}
      style={color ? { backgroundColor: color } : undefined}
    >
      {letters}
    </span>
  );
}
