import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

import { MAX_ATTACHMENT_BYTES } from "./src/lib/attachments";
import { STATIC_SECURITY_HEADERS } from "./src/lib/security";

const webDir = path.dirname(fileURLToPath(import.meta.url));

/**
 * In production the reverse proxy sends /v1 and /mcp to the API and everything else here. Without one (locally, in
 * Playwright, in deploy/hub/docker-compose.dev.yml) the web serves /v1 itself by forwarding it to
 * EVO_HUB_API_INTERNAL_URL, so the browser sees one origin either way. The rewrite is fixed when the app is built:
 * a build without the variable has none. The image (web/Dockerfile) builds with http://api:8080 by default.
 */
const apiUrl = process.env.EVO_HUB_API_INTERNAL_URL?.trim().replace(/\/+$/, "");

/**
 * The routes of the tests' fixtures, named `page.fixture.tsx` and `layout.fixture.tsx` (the native page of the core
 * plugin notes, src/components/plugin-notes): a route only in a build with EVO_WEB_FIXTURES=1, which Playwright's
 * build sets. A production build, the image's, has none of them.
 */
const fixtures = process.env.EVO_WEB_FIXTURES === "1";
const PAGE_EXTENSIONS = ["tsx", "ts", "jsx", "js"];

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: webDir,
  turbopack: { root: webDir },
  poweredByHeader: false,
  reactStrictMode: true,
  typedRoutes: true,
  pageExtensions: fixtures ? ["fixture.tsx", ...PAGE_EXTENSIONS] : PAGE_EXTENSIONS,
  agentRules: false, // `next dev` would otherwise write AGENTS.md and CLAUDE.md into web/
  // The web hands a request's body on to the /v1 rewrite after buffering it, and cuts it at 10 MB by default: a file
  // attached to a message goes up to the hub's limit (50 MiB) and a little over, so the hub's own 413 says why.
  experimental: { proxyClientMaxBodySize: MAX_ATTACHMENT_BYTES + 1024 * 1024 },
  async headers() {
    return [{ source: "/:path*", headers: [...STATIC_SECURITY_HEADERS] }];
  },
  async rewrites() {
    return apiUrl ? { beforeFiles: [{ source: "/v1/:path*", destination: `${apiUrl}/v1/:path*` }] } : [];
  },
};

export default createNextIntlPlugin("./src/i18n/request.ts")(nextConfig);
