import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import { headers } from "next/headers";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { Providers } from "@/components/providers";
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

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("app");
  return {
    title: { default: t("name"), template: `%s | ${t("name")}` },
    description: t("description"),
    robots: { index: false, follow: false },
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
  const requestHeaders = await headers();
  const nonce = requestHeaders.get("x-nonce") ?? undefined; // set by src/proxy.ts for every page
  return (
    <html lang={locale} className={`${sans.variable} ${mono.variable}`} suppressHydrationWarning>
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
