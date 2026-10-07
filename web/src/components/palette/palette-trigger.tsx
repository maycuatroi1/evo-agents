"use client";

import { Search } from "lucide-react";
import { useTranslations } from "next-intl";

import { shortcutText } from "@/components/shell/shortcuts";
import { Kbd } from "@/components/ui/kbd";
import { useModifierKey } from "@/lib/keyboard";

import { preloadPalette, usePalette } from "./palette-context";

/**
 * The kit's search trigger in the top bar: "Search or jump to" with its key, opening the command palette. 280 px from
 * xl, 224 px at lg; below lg the words and the key fold into the button's name and its magnifier, a 32 px square (44 px
 * under 768 px), so the breadcrumb keeps its room beside the sidebar. The key reads ⌘K on Apple devices and Ctrl K
 * elsewhere, once the browser says which.
 */
export function PaletteTrigger() {
  const t = useTranslations("palette");
  const { open, openPalette } = usePalette();
  const modifier = useModifierKey();
  return (
    <button
      type="button"
      onClick={(event) => openPalette(event.currentTarget)}
      onPointerEnter={preloadPalette}
      onFocus={preloadPalette}
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-keyshortcuts="Meta+K Control+K"
      className="inline-flex h-8 w-56 shrink-0 cursor-pointer items-center gap-2 rounded-sm border border-border-strong bg-background pr-1.5 pl-2.5 text-[13px] text-fg-subtle transition-colors hover:bg-accent hover:text-muted-foreground aria-expanded:bg-accent max-lg:w-8 max-lg:justify-center max-lg:p-0 max-md:size-11 xl:w-70"
      data-testid="palette-trigger"
    >
      <Search className="size-4 max-md:size-5" aria-hidden="true" />
      <span className="truncate max-lg:sr-only">{t("trigger")}</span>
      {modifier ? (
        <Kbd className="ml-auto max-lg:hidden" aria-hidden="true" data-testid="palette-trigger-key">
          {shortcutText("palette", modifier)}
        </Kbd>
      ) : null}
    </button>
  );
}
