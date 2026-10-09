"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { CommandPaletteProvider } from "@/components/palette/palette-context";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

import { AppSidebar } from "./app-sidebar";
import { ShortcutsProvider } from "./shortcuts";
import { SiteHeader } from "./site-header";

export function AppShell({ sidebarOpen, children }: { sidebarOpen: boolean; children: ReactNode }) {
  const t = useTranslations("app");
  return (
    <SidebarProvider defaultOpen={sidebarOpen}>
      {/* Outside the palette's provider: the palette lists the shortcuts dialog and the keys of its items. */}
      <ShortcutsProvider>
        <CommandPaletteProvider>
          <a
            href="#main"
            className="sr-only z-50 rounded-sm bg-primary px-3 py-2 text-sm font-medium text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
          >
            {t("skipToContent")}
          </a>
          <AppSidebar />
          {/* min-w-0: the inset is a flex item beside the sidebar, and without it a wide table's natural width would
              widen it, and <main> with it, past the screen. Tables scroll in their own region instead. */}
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
        </CommandPaletteProvider>
      </ShortcutsProvider>
    </SidebarProvider>
  );
}
