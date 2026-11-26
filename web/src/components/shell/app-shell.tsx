"use client";

import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { CommandPaletteProvider } from "@/components/palette/palette-context";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

import { AppSidebar } from "./app-sidebar";
import { parseNavGroups } from "./nav";
import { ShortcutsProvider } from "./shortcuts";
import { SiteHeader } from "./site-header";

type Props = {
  sidebarOpen: boolean;
  /** The cookie of the sidebar's open folds, as the server read it; none before the visitor opens one. */
  navGroups?: string;
  children: ReactNode;
};

/**
 * The signed-in shell (web/DESIGN.md, Spacing and layout): the sidebar, the top bar and the content frame on every page
 * but Home. Home (`/`, web/DESIGN.md, Chat home) is a workspace with an agent that draws its own rail, so it has no top
 * bar and the sidebar is a sheet at every width, opened from the rail's menu button or Ctrl or Cmd + B. The providers of
 * the palette and the shortcuts stay at the same place in the tree on every page, so Cmd K and the keys work on Home
 * too and survive the move between Home and another page.
 */
export function AppShell({ sidebarOpen, navGroups, children }: Props) {
  const t = useTranslations("app");
  const home = usePathname() === "/";
  return (
    <SidebarProvider defaultOpen={sidebarOpen} sheet={home}>
      {/* Outside the palette's provider: the palette lists the shortcuts dialog and the keys of its items. */}
      <ShortcutsProvider>
        <CommandPaletteProvider>
          <a
            href="#main"
            className="sr-only z-50 rounded-sm bg-primary px-3 py-2 text-sm font-medium text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
          >
            {t("skipToContent")}
          </a>
          <AppSidebar openGroups={parseNavGroups(navGroups)} />
          {home ? (
            <main id="main" tabIndex={-1} className="flex min-w-0 flex-1 outline-none">
              {children}
            </main>
          ) : (
            // min-w-0: the inset is a flex item beside the sidebar, and without it a wide table's natural width would
            // widen it, and <main> with it, past the screen. Tables scroll in their own region instead.
            <SidebarInset className="min-w-0">
              <SiteHeader />
              {/* The content column: the whole width between the sidebar and the window's right edge, less a 24 px
                  gutter (16 px on phones), on every page. No cap: a page that would stretch thin on a wide screen lays
                  itself out for the width (more columns, a side column), rather than the shell narrowing them all. */}
              <main id="main" tabIndex={-1} className="flex-1 p-4 outline-none md:p-6">
                <div className="flex w-full flex-col gap-6" data-testid="content-frame">
                  {children}
                </div>
              </main>
            </SidebarInset>
          )}
        </CommandPaletteProvider>
      </ShortcutsProvider>
    </SidebarProvider>
  );
}
