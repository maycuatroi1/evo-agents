import { BookOpen, ListChecks, Network, Sparkles, TriangleAlert } from "lucide-react";
import type { Metadata } from "next";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { BrandMark, GitHubMark } from "@/components/brand";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { call } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { serverApi } from "@/lib/api/server";
import { API_LOGIN_PATH, hasSession, SESSION_COOKIE } from "@/lib/config";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("login");
  return { title: t("title") };
}

type Availability = "ready" | "unconfigured" | "unreachable";

async function availability(): Promise<Availability> {
  try {
    const config = await call((await serverApi()).GET("/v1/auth/config"));
    return config.web_login ? "ready" : "unconfigured";
  } catch (error) {
    return isApiError(error) && error.status === 503 ? "unconfigured" : "unreachable";
  }
}

async function signedIn(): Promise<boolean> {
  if (!hasSession((await cookies()).get(SESSION_COOKIE)?.value)) return false;
  try {
    await call((await serverApi()).GET("/v1/auth/whoami"));
    return true;
  } catch {
    return false; // expired, revoked or API down: stay here
  }
}

/** One icon per concept, the same ones the sidebar uses (web/DESIGN.md, Iconography). */
const FEATURES = [
  { icon: BookOpen, label: "Memories" },
  { icon: Sparkles, label: "Skills" },
  { icon: ListChecks, label: "Plans" },
  { icon: Network, label: "Knowledge graph" },
] as const;

export default async function LoginPage() {
  if (await signedIn()) redirect("/");
  const [t, tApp, status] = await Promise.all([getTranslations("login"), getTranslations("app"), availability()]);

  return (
    <main className="grid min-h-svh lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <section className="hidden flex-col justify-between border-r bg-surface-sunken p-10 lg:flex">
        <div className="flex items-center gap-3">
          <BrandMark />
          <span className="text-lg font-semibold tracking-tight">{tApp("name")}</span>
        </div>
        <div className="flex max-w-md flex-col gap-6">
          <p className="text-2xl leading-snug font-semibold text-balance">{tApp("description")}</p>
          <ul className="grid grid-cols-2 gap-3 text-sm">
            {FEATURES.map(({ icon: Icon, label }) => (
              <li key={label} className="flex items-center gap-2 rounded-sm border bg-card px-3 py-2.5 shadow-raised">
                <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                {label}
              </li>
            ))}
          </ul>
        </div>
        <p className="text-sm text-muted-foreground">{t("access")}</p>
      </section>

      <section className="flex items-center justify-center px-4 py-12 sm:px-8">
        <div className="flex w-full max-w-sm flex-col gap-8">
          <div className="flex items-center gap-3 lg:hidden">
            <BrandMark />
            <span className="text-lg font-semibold tracking-tight">{tApp("name")}</span>
          </div>
          <div className="flex flex-col gap-2">
            <h1 className="text-2xl font-semibold tracking-tight text-balance">{t("heading")}</h1>
            <p className="text-sm text-pretty text-muted-foreground">{t("description")}</p>
          </div>

          {status === "ready" ? (
            <Button asChild size="lg" className="h-11 w-full text-base">
              <a href={API_LOGIN_PATH} data-testid="login-github">
                <GitHubMark className="size-5" />
                {t("github")}
              </a>
            </Button>
          ) : (
            <div className="flex flex-col gap-4">
              <Alert variant="destructive" data-testid="login-unavailable">
                <TriangleAlert aria-hidden="true" />
                <AlertDescription>{status === "unconfigured" ? t("notConfigured") : t("unreachable")}</AlertDescription>
              </Alert>
              <Button size="lg" className="h-11 w-full text-base" disabled>
                <GitHubMark className="size-5" />
                {t("github")}
              </Button>
            </div>
          )}

          <div className="flex flex-col gap-2 border-t pt-6 text-sm text-muted-foreground">
            <p className="lg:hidden">{t("access")}</p>
            <p>
              {t.rich("cli", {
                code: (chunks) => (
                  <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs whitespace-nowrap text-foreground">
                    {chunks}
                  </code>
                ),
              })}
            </p>
          </div>
        </div>
      </section>
    </main>
  );
}
