import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Branding } from "@/lib/hub-settings";
import { serverBranding } from "@/lib/hub-settings-server";

import manifest from "./manifest";

vi.mock("next-intl/server", () => ({
  getTranslations: async () => (key: string) => ({ name: "Evo Agent Hub", description: "The team's store." })[key],
}));
vi.mock("@/lib/hub-settings-server", () => ({ serverBranding: vi.fn() }));

const publicDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../public");

function branding(overrides: Partial<Branding>): Branding {
  return { name: null, logo_url: null, favicon_url: null, colors: {}, version: 1, ...overrides };
}

/** Width and height of a PNG, from its IHDR chunk. */
function pngSize(file: string): string {
  const bytes = readFileSync(path.join(publicDir, file));
  return `${bytes.readUInt32BE(16)}x${bytes.readUInt32BE(20)}`;
}

describe("manifest", () => {
  beforeEach(() => vi.mocked(serverBranding).mockResolvedValue(null));

  it("installs the hub as a standalone app with the web's name and tiles", async () => {
    const app = await manifest();
    expect(app).toMatchObject({ id: "/", name: "Evo Agent Hub", start_url: "/", scope: "/", display: "standalone" });
    expect(app.icons?.map((icon) => icon.src)).toEqual(["/icon.svg", "/icon-192.png", "/icon-512.png"]);
  });

  it("declares each PNG tile at the size it has", async () => {
    const app = await manifest();
    for (const icon of app.icons ?? []) {
      if (icon.type === "image/png") expect(pngSize(icon.src.slice(1)), icon.src).toBe(icon.sizes);
    }
  });

  it("follows the branding's name, and its favicon in place of the evo-agents mark", async () => {
    vi.mocked(serverBranding).mockResolvedValue(branding({ name: "Northwind Hub", favicon_url: "/v1/branding/favicon?v=ab12" }));
    const app = await manifest();
    expect(app).toMatchObject({ name: "Northwind Hub", short_name: "Northwind Hub" });
    expect(app.icons).toEqual([{ src: "/v1/branding/favicon?v=ab12" }]);
  });

  it("keeps the web's tiles when the branding sets only a name", async () => {
    vi.mocked(serverBranding).mockResolvedValue(branding({ name: "Lumen" }));
    expect((await manifest()).icons).toHaveLength(3);
  });
});
