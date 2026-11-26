"use client";

import { CalendarCheck, Search } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { InboxBell } from "@/components/inbox/inbox-bell";
import { LiveIndicator } from "@/components/live/live-indicator";
import { preloadPalette, usePalette } from "@/components/palette/palette-context";
import { shortcutText } from "@/components/shell/shortcuts";
import { UserMenu } from "@/components/shell/user-menu";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { useModifierKey } from "@/lib/keyboard";

/**
 * The foot of Home's rail (web/DESIGN.md, Chat home), what the top bar holds on the other pages: the command palette
 * with its key, the Inbox with its count, Today, whether the page is current (shown only when it is not), and the
 * account. Composed here, in the app, since the chat feature may not reach into the palette's or the inbox's.
 */
export function HomeRailFoot() {
  const t = useTranslations("chatHome");
  const { open, openPalette } = usePalette();
  const modifier = useModifierKey();
  return (
    <>
      <div className="flex min-w-0 items-center gap-1">
        <button
          type="button"
          onClick={(event) => openPalette(event.currentTarget)}
          onPointerEnter={preloadPalette}
          onFocus={preloadPalette}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-keyshortcuts="Meta+K Control+K"
          aria-label={t("rail.palette")}
          className="inline-flex h-8 min-w-0 flex-1 cursor-pointer items-center gap-2 rounded-sm border border-border-strong bg-background pr-1.5 pl-2.5 text-[13px] text-fg-subtle transition-colors hover:bg-accent hover:text-muted-foreground aria-expanded:bg-accent max-md:h-11"
          data-testid="palette-trigger"
        >
          <Search className="size-4 shrink-0" aria-hidden="true" />
          <span className="truncate">{t("rail.palette")}</span>
          {modifier ? (
            <Kbd className="ml-auto" aria-hidden="true" data-testid="palette-trigger-key">
              {shortcutText("palette", modifier)}
            </Kbd>
          ) : null}
        </button>
        <LiveIndicator />
        <InboxBell />
        <Button asChild variant="ghost" size="icon-sm" className="shrink-0 max-md:size-11">
          <Link href={"/today" as Route} aria-label={t("todayLink")} title={t("todayLink")} data-testid="chat-home-today">
            <CalendarCheck aria-hidden="true" />
          </Link>
        </Button>
      </div>
      <UserMenu />
    </>
  );
}
