"use client";

import { useState } from "react";

import { cn } from "@/lib/utils";

import { inkOn } from "./provider-cards";
import LOGO_FILES from "./provider-logos.json";
import type { CatalogProvider } from "./provider-queries";

/** The file under public/provider-logos of each provider of the catalog with a logo (scripts/generate_provider_logos.py). */
const LOGOS: Readonly<Record<string, string>> = LOGO_FILES;

/**
 * The mark of a provider of the catalog: its logo on a white tile, the same in both themes as a logo is drawn for a
 * light ground; else, for a provider that publishes none (or a logo that fails to load), its abbreviation (`text_icon`)
 * on its catalog colour, the ink white or black, whichever contrasts more. Decorative, as the provider's name is text
 * beside it; it says who the provider is, never how its keys are doing.
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
  const [broken, setBroken] = useState<string | null>(null);
  const box = size === "md" ? "size-9 rounded-md" : "size-7 rounded-[6px]";
  const file = LOGOS[id];
  if (file && broken !== file) {
    const px = size === "md" ? 24 : 18;
    return (
      <span
        aria-hidden="true"
        data-provider-mark={id}
        data-provider-logo={file}
        className={cn("inline-flex shrink-0 items-center justify-center border bg-white", box, className)}
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- a small static file of public/, nothing to optimise */}
        <img
          src={`/provider-logos/${file}`}
          alt=""
          width={px}
          height={px}
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => setBroken(file)}
          className={cn("object-contain", size === "md" ? "size-6" : "size-[18px]")}
        />
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
