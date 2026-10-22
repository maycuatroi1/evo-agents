import type { Metadata } from "next";
import { cookies } from "next/headers";
import { getTranslations } from "next-intl/server";

import { OG_IMAGE } from "@/components/product/images";
import { ProductPage } from "@/components/product/product-page";
import { call } from "@/lib/api/client";
import { serverApi } from "@/lib/api/server";
import { API_LOGIN_PATH, hasSession, LOGIN_PATH, SESSION_COOKIE } from "@/lib/config";
import { requestOrigin } from "@/lib/request-origin";

/**
 * The one page search engines may index (every other keeps the root layout's noindex). Its canonical URL is `/`,
 * where src/proxy.ts shows it to a visitor without a session; the OpenGraph image is a file of this origin.
 */
export async function generateMetadata(): Promise<Metadata> {
  const [t, origin] = await Promise.all([getTranslations("product.meta"), requestOrigin()]);
  const title = t("title");
  const description = t("description");
  const image = { ...OG_IMAGE, alt: t("imageAlt") };
  return {
    metadataBase: new URL(origin),
    title: { absolute: title },
    description,
    robots: { index: true, follow: true },
    alternates: { canonical: "/" },
    openGraph: { type: "website", url: "/", siteName: "evo-agents", title, description, images: [image] },
    twitter: { card: "summary_large_image", title, description, images: [image] },
  };
}

/** Whether the hub signs people in with GitHub now (GET /v1/auth/config, which needs no session). */
async function loginReady(): Promise<boolean> {
  try {
    return (await call((await serverApi()).GET("/v1/auth/config"))).web_login;
  } catch {
    return false;
  }
}

export default async function AboutRoute() {
  const [ready, origin, cookieStore] = await Promise.all([loginReady(), requestOrigin(), cookies()]);
  return (
    <ProductPage
      signInHref={ready ? API_LOGIN_PATH : LOGIN_PATH}
      member={hasSession(cookieStore.get(SESSION_COOKIE)?.value)}
      hubUrl={origin}
    />
  );
}
