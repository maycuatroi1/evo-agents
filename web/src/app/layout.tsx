import type { Metadata, Viewport } from "next";
import { Be_Vietnam_Pro, JetBrains_Mono } from "next/font/google";
import { headers } from "next/headers";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { Providers } from "@/components/providers";

import "./globals.css";

const sans = Be_Vietnam_Pro({
  subsets: ["latin", "vietnamese"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-be-vietnam-pro",
  display: "swap",
});

const mono = JetBrains_Mono({
  subsets: ["latin", "vietnamese"],
  variable: "--font-jetbrains-mono",
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
    { media: "(prefers-color-scheme: light)", color: "#f8fafc" },
    { media: "(prefers-color-scheme: dark)", color: "#020617" },
  ],
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const locale = await getLocale();
  const nonce = (await headers()).get("x-nonce") ?? undefined; // set by src/proxy.ts for every page
  return (
    <html lang={locale} className={`${sans.variable} ${mono.variable}`} suppressHydrationWarning>
      <body className="min-h-svh">
        <NextIntlClientProvider>
          <Providers nonce={nonce}>{children}</Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
