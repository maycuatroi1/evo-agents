import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const root = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.join(root, "src"),
      "server-only": path.join(root, "src/test/empty.ts"),
    },
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}", "messages/**/*.test.ts", "scripts/**/*.test.mjs"],
    environment: "node",
    setupFiles: ["src/test/setup.ts"],
    restoreMocks: true,
  },
});
