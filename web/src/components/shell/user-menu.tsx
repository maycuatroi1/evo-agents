"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronsUpDown, Languages, Loader2, LogOut, Monitor, Moon, Sun, TriangleAlert } from "lucide-react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useState } from "react";

import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { SidebarMenu, SidebarMenuButton, SidebarMenuItem, useSidebar } from "@/components/ui/sidebar";
import { LOCALES } from "@/i18n/locales";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { LOCALE_COOKIE, LOGIN_PATH } from "@/lib/config";
import { projectsQuery, whoamiQuery } from "@/lib/queries";
import { initials } from "@/lib/utils";

import { useCurrentProject } from "./project-switcher";
import { roleLabelKey } from "./role-badge";

const YEAR = 60 * 60 * 24 * 365;

/** Revoke the web session on the API (a cookie write, so it carries the CSRF header), then go to sign in. */
async function signOut(): Promise<void> {
  const api = browserApi();
  const { csrf, header } = await call(api.GET("/v1/auth/web/csrf"));
  await call(api.POST("/v1/auth/web/logout", { headers: { [header]: csrf } }));
}

export function UserMenu() {
  const t = useTranslations("userMenu");
  const tRoles = useTranslations("roles");
  const tLocales = useTranslations("locales");
  const locale = useLocale();
  const router = useRouter();
  const { theme, setTheme } = useTheme();
  const { isMobile } = useSidebar();
  const project = useCurrentProject();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const { data: projects } = useQuery(projectsQuery(browserApi));
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);

  if (!me) return null;
  const current = projects?.find((p) => p.name === project) ?? null;

  const logout = async () => {
    setPending(true);
    setFailed(false);
    try {
      await signOut();
      window.location.assign(LOGIN_PATH);
    } catch (error) {
      if (isApiError(error) && error.kind === "unauthorized") {
        window.location.assign(LOGIN_PATH); // already signed out
        return;
      }
      setFailed(true);
      setPending(false);
    }
  };

  const changeLocale = (next: string) => {
    const secure = window.location.protocol === "https:" ? "; secure" : "";
    document.cookie = `${LOCALE_COOKIE}=${next}; path=/; max-age=${YEAR}; samesite=lax${secure}`;
    router.refresh();
  };

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        {/* Not modal: a menu leaves the page readable and reachable instead of hiding it from assistive tech. */}
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton
              size="lg"
              data-testid="user-menu"
              aria-label={t("trigger", { login: me.login })}
              className="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
            >
              <Avatar className="size-8 rounded-md">
                <AvatarFallback className="rounded-md bg-secondary font-mono text-xs font-medium">
                  {initials(me.login)}
                </AvatarFallback>
              </Avatar>
              <span className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-semibold">{me.login}</span>
                <span className="truncate text-xs text-muted-foreground">
                  {me.admin ? t("hubAdmin") : t("member")}
                </span>
              </span>
              <ChevronsUpDown className="ml-auto size-4" aria-hidden="true" />
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            className="w-(--radix-dropdown-menu-trigger-width) min-w-64 rounded-md"
            side={isMobile ? "top" : "right"}
            align="end"
            sideOffset={4}
            data-testid="user-menu-content"
          >
            <DropdownMenuLabel className="flex flex-col gap-1 py-2 font-normal">
              <span className="truncate text-sm font-semibold" data-testid="user-login">
                {me.login}
              </span>
              <span className="text-xs text-muted-foreground" data-testid="user-role">
                {me.admin ? t("hubAdmin") : t("member")}
              </span>
              <span className="text-xs text-muted-foreground">{t("grants", { count: me.grants.length })}</span>
              {current ? (
                <span className="text-xs text-muted-foreground" data-testid="user-project-role">
                  {current.role
                    ? t("projectRole", {
                        project: current.name,
                        role: tRoles(roleLabelKey(current.role)),
                        level: current.max_level ?? "",
                      })
                    : t("projectRoleNone", { project: current.name })}
                </span>
              ) : null}
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuSub>
                <DropdownMenuSubTrigger>
                  <Sun aria-hidden="true" />
                  {t("theme")}
                </DropdownMenuSubTrigger>
                <DropdownMenuSubContent>
                  <DropdownMenuRadioGroup value={theme ?? "system"} onValueChange={setTheme}>
                    <DropdownMenuRadioItem value="light">
                      <Sun aria-hidden="true" />
                      {t("themeLight")}
                    </DropdownMenuRadioItem>
                    <DropdownMenuRadioItem value="dark">
                      <Moon aria-hidden="true" />
                      {t("themeDark")}
                    </DropdownMenuRadioItem>
                    <DropdownMenuRadioItem value="system">
                      <Monitor aria-hidden="true" />
                      {t("themeSystem")}
                    </DropdownMenuRadioItem>
                  </DropdownMenuRadioGroup>
                </DropdownMenuSubContent>
              </DropdownMenuSub>
              <DropdownMenuSub>
                <DropdownMenuSubTrigger>
                  <Languages aria-hidden="true" />
                  {t("language")}
                </DropdownMenuSubTrigger>
                <DropdownMenuSubContent>
                  <DropdownMenuRadioGroup value={locale} onValueChange={changeLocale}>
                    {LOCALES.map((code) => (
                      <DropdownMenuRadioItem key={code} value={code} lang={code}>
                        {tLocales(code)}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuSubContent>
              </DropdownMenuSub>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              data-testid="logout"
              disabled={pending}
              onSelect={(event) => {
                event.preventDefault(); // keep the menu open to show progress or failure
                void logout();
              }}
            >
              {pending ? <Loader2 className="animate-spin" aria-hidden="true" /> : <LogOut aria-hidden="true" />}
              {pending ? t("loggingOut") : t("logout")}
            </DropdownMenuItem>
            {failed ? (
              <p role="alert" className="flex items-start gap-2 px-2 py-1.5 text-xs text-danger">
                <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                {t("logoutFailed")}
              </p>
            ) : null}
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  );
}
