"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

import { AppSidebar } from "./app-sidebar";
import { SiteHeader } from "./site-header";

export function AppShell({ sidebarOpen, children }: { sidebarOpen: boolean; children: ReactNode }) {
  const t = useTranslations("app");
  return (
    <SidebarProvider defaultOpen={sidebarOpen}>
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
        {/* The kit's content column: at most 1280 px, a 24 px gutter (16 px on phones). */}
        <main id="main" tabIndex={-1} className="flex-1 p-4 outline-none md:p-6">
          <div className="mx-auto flex w-full max-w-7xl flex-col gap-6">{children}</div>
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}
