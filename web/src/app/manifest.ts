import type { MetadataRoute } from "next";
import { getTranslations } from "next-intl/server";

import { serverBranding } from "@/lib/hub-settings-server";

/**
 * The web app manifest at /manifest.webmanifest, which lets a browser install the hub as an app of its own: a window
 * without the browser's tabs, an icon in the Dock or the taskbar, an entry of the app switcher. Whoever installs it
 * does so from their hub's own address, so each hub is its own app.
 *
 * The browser fetches the manifest without the session cookie, so src/proxy.ts lets it and its icons through without
 * a session, and the branding it reads (GET /v1/branding) needs no credential either. The name and the icon follow
 * the branding as the root layout's title and favicon do: a branded favicon replaces the web's tiles, so a branded hub
 * never shows the evo-agents mark. The tiles public/icon-192.png and public/icon-512.png are public/icon.svg in its
 * light-theme colours, drawn with `resvg -w 512 -h 512 public/icon.svg public/icon-512.png`.
 */
export default async function manifest(): Promise<MetadataRoute.Manifest> {
  const [t, branding] = await Promise.all([getTranslations("app"), serverBranding()]);
  const name = branding?.name ?? t("name");
  return {
    id: "/",
    name,
    short_name: name,
    description: t("description"),
    start_url: "/",
    scope: "/",
    display: "standalone",
    icons: branding?.favicon_url
      ? [{ src: branding.favicon_url }]
      : [
          { src: "/icon.svg", sizes: "any", type: "image/svg+xml" },
          { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
        ],
  };
}
