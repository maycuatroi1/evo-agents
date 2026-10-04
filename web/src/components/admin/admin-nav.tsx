"use client";

import { useQuery } from "@tanstack/react-query";
import { KeyRound, LayoutDashboard, type LucideIcon, ScrollText, Users } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";

import { isActive } from "@/components/shell/nav";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

export type AdminSection = "overview" | "members" | "tokens" | "audit";

export const ADMIN_SECTIONS: readonly { id: AdminSection; href: Route; icon: LucideIcon }[] = [
  { id: "overview", href: "/admin", icon: LayoutDashboard },
  { id: "members", href: "/admin/members", icon: Users },
  { id: "tokens", href: "/admin/tokens", icon: KeyRound },
  { id: "audit", href: "/admin/audit", icon: ScrollText },
];

/**
 * The admin area's own navigation, shown to hub admins only. Hiding it is a courtesy: the API refuses every admin
 * route to anyone else, and the pages show that refusal.
 */
export function AdminNav() {
  const t = useTranslations("admin.nav");
  const pathname = usePathname();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  if (!me?.admin) return null;
  return (
    <nav aria-label={t("label")} className="-mt-2 border-b" data-testid="admin-nav">
      <ul className="-mb-px flex gap-1 overflow-x-auto">
        {ADMIN_SECTIONS.map((section) => {
          const active = isActive(pathname, section.href, section.id === "overview");
          return (
            <li key={section.id} className="shrink-0">
              <Link
                href={section.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "inline-flex h-11 items-center gap-2 rounded-t-md border-b-2 px-2.5 text-sm font-medium transition-colors sm:px-3",
                  active
                    ? "border-primary text-foreground"
                    : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
                )}
              >
                <section.icon className="hidden size-4 sm:block" aria-hidden="true" />
                {t(section.id)}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
