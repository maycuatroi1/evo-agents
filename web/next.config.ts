import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

import { STATIC_SECURITY_HEADERS } from "./src/lib/security";

const webDir = path.dirname(fileURLToPath(import.meta.url));

/**
 * In production the reverse proxy sends /v1 and /mcp to the API and everything else here. Without one (locally, in
 * Playwright, in deploy/hub/docker-compose.dev.yml) the web serves /v1 itself by forwarding it to
 * EVO_HUB_API_INTERNAL_URL, so the browser sees one origin either way. The rewrite is fixed when the app is built:
 * a build without the variable has none. The image (web/Dockerfile) builds with http://api:8080 by default.
 */
const apiUrl = process.env.EVO_HUB_API_INTERNAL_URL?.trim().replace(/\/+$/, "");

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: webDir,
  turbopack: { root: webDir },
  poweredByHeader: false,
  reactStrictMode: true,
  typedRoutes: true,
  agentRules: false, // `next dev` would otherwise write AGENTS.md and CLAUDE.md into web/
  async headers() {
    return [{ source: "/:path*", headers: [...STATIC_SECURITY_HEADERS] }];
  },
  async rewrites() {
    return apiUrl ? { beforeFiles: [{ source: "/v1/:path*", destination: `${apiUrl}/v1/:path*` }] } : [];
  },
};

export default createNextIntlPlugin("./src/i18n/request.ts")(nextConfig);
