import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import { headers } from "next/headers";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { Providers } from "@/components/providers";
import { brandingCss } from "@/lib/hub-settings";
import { serverBranding } from "@/lib/hub-settings-server";
import { mobileHint } from "@/lib/mobile-hint";

import "./globals.css";

// IBM Plex for the interface (Sans) and for data, logs and the terminal (Mono). Both carry the Vietnamese subset, so
// the Vietnamese people write in plans, evidence and memories renders in the same faces as the English around it.
const sans = IBM_Plex_Sans({
  subsets: ["latin", "latin-ext", "vietnamese"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-sans",
  display: "swap",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin", "latin-ext", "vietnamese"],
  weight: ["400", "500"],
  variable: "--font-plex-mono",
  display: "swap",
});

/**
 * The title and the favicon follow the hub's branding when it sets a name or a favicon (GET /v1/branding); without
 * one they are the web's own, public/icon.svg being the mark.
 */
export async function generateMetadata(): Promise<Metadata> {
  const [t, branding] = await Promise.all([getTranslations("app"), serverBranding()]);
  const name = branding?.name ?? t("name");
  return {
    title: { default: name, template: `%s | ${name}` },
    description: t("description"),
    robots: { index: false, follow: false },
    icons: { icon: branding?.favicon_url ?? { url: "/icon.svg", type: "image/svg+xml" } },
  };
}

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f5f6f8" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0c10" },
  ],
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const locale = await getLocale();
  const [requestHeaders, branding] = await Promise.all([headers(), serverBranding()]);
  const nonce = requestHeaders.get("x-nonce") ?? undefined; // set by src/proxy.ts for every page
  const colors = brandingCss(branding?.colors); // the branding's colours over the tokens; none without branding
  return (
    <html lang={locale} className={`${sans.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>{colors ? <style data-testid="hub-branding">{colors}</style> : null}</head>
      <body className="min-h-svh">
        <NextIntlClientProvider>
          <Providers nonce={nonce} mobile={mobileHint(requestHeaders)}>
            {children}
          </Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
