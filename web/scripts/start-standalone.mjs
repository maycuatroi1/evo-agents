// Runs the standalone server the production image runs (`node server.js`), after copying in the static assets
// `next build` leaves outside .next/standalone. PORT and HOSTNAME choose where it listens.
import { spawn } from "node:child_process";
import { cpSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const standalone = path.join(webDir, ".next", "standalone");
const server = path.join(standalone, "server.js");
if (!existsSync(server)) {
  console.error("no .next/standalone/server.js: run `pnpm build` first");
  process.exit(1);
}
cpSync(path.join(webDir, ".next", "static"), path.join(standalone, ".next", "static"), { recursive: true });
if (existsSync(path.join(webDir, "public"))) {
  cpSync(path.join(webDir, "public"), path.join(standalone, "public"), { recursive: true });
}

const child = spawn(process.execPath, [server], { stdio: "inherit", env: process.env });
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill(signal));
child.on("exit", (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
