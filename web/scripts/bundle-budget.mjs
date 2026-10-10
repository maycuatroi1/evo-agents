// First Load JS budget of every route of the web.
//
// The First Load JS of a route is the gzip size of the chunks the production build lists for it: the root main files
// of the build manifest plus the entry JS files of the route's page and layouts in its client reference manifest.
// `next build` (Turbopack) writes that list to .next/diagnostics/route-bundle-stats.json, with sizes before gzip; this
// script gzips each listed chunk (level 9, as gzip-size did for the size column Next used to print) and adds them up.
//
// bundle-budget.json holds one budget per route, in kB (1 kB = 1000 bytes). The script fails when a route is over its
// budget, when the build has a route the file lacks, or when the file lists a route the build does not have. It also
// fails when the file raises a budget or adds a route compared with the file on the base ref (origin/main by default)
// and that entry does not say why in its own "reason".
//
//   node scripts/bundle-budget.mjs                  measure .next, check the budgets and the base ref's file
//   node scripts/bundle-budget.mjs --base <ref>     the same, against <ref>; fails when <ref> cannot be read
//   node scripts/bundle-budget.mjs --chunks <route> the route's chunks, largest first
//   node scripts/bundle-budget.mjs --write          set every budget to what the build measures (keeps the reasons)
//
// Run it after `pnpm build`. CI runs it in the first web-e2e shard, on the build Playwright served.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { gzipSync } from "node:zlib";

export const BUDGET_FILE = "bundle-budget.json";
export const STATS_FILE = path.join(".next", "diagnostics", "route-bundle-stats.json");
export const DEFAULT_BASE = "origin/main";

const BUDGET_COMMENT =
  "First Load JS budget of each route in kB (gzip, 1 kB = 1000 bytes), checked by scripts/bundle-budget.mjs. " +
  "Raising a budget or adding a route needs a reason in that route's entry; lowering one needs none.";

/** kB with one decimal, rounded up so a budget set from a measure holds that measure. */
export function toKb(bytes) {
  return Math.ceil(bytes / 100) / 10;
}

function formatKb(kb) {
  return `${kb.toFixed(1)} kB`;
}

/**
 * The routes of the build in `webDir` with their chunks and gzip sizes, largest route first. Throws when the build
 * left no route-bundle-stats.json or a chunk it lists is missing.
 */
export function measureRoutes(webDir) {
  const statsPath = path.join(webDir, STATS_FILE);
  if (!existsSync(statsPath)) {
    throw new Error(
      `${STATS_FILE} not found: run \`pnpm build\` first (next build with Turbopack writes it; a build with --webpack does not)`,
    );
  }
  const rows = JSON.parse(readFileSync(statsPath, "utf8"));
  const gzipCache = new Map();
  const gzipBytes = (file) => {
    if (!gzipCache.has(file)) {
      const full = path.resolve(webDir, file);
      if (!existsSync(full)) throw new Error(`${file}, listed in ${STATS_FILE}, does not exist: build again`);
      gzipCache.set(file, gzipSync(readFileSync(full), { level: 9 }).length);
    }
    return gzipCache.get(file);
  };
  return rows
    .map((row) => {
      const chunks = [...new Set(row.firstLoadChunkPaths ?? [])]
        .filter((file) => file.endsWith(".js"))
        .map((file) => ({ file, bytes: gzipBytes(file) }))
        .sort((a, b) => b.bytes - a.bytes);
      return { route: row.route, bytes: chunks.reduce((sum, chunk) => sum + chunk.bytes, 0), chunks };
    })
    .sort((a, b) => b.bytes - a.bytes || a.route.localeCompare(b.route));
}

/** The budget file's routes, or throws with the reason the file is unusable. */
export function parseBudgets(text, where) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch (error) {
    throw new Error(`${where} is not valid JSON: ${error.message}`);
  }
  if (!doc || typeof doc !== "object" || !doc.routes || typeof doc.routes !== "object") {
    throw new Error(`${where} has no "routes" object`);
  }
  for (const [route, entry] of Object.entries(doc.routes)) {
    if (!entry || typeof entry.kB !== "number" || !(entry.kB > 0)) {
      throw new Error(`${where}: the entry of ${route} needs a positive number "kB"`);
    }
    if (entry.reason !== undefined && typeof entry.reason !== "string") {
      throw new Error(`${where}: the "reason" of ${route} must be a string`);
    }
  }
  return doc.routes;
}

function chunksHint(route) {
  return `see its largest chunks with \`node scripts/bundle-budget.mjs --chunks '${route}'\``;
}

/** Problems of the measured routes against the budgets, one message each. */
export function checkBudgets(measured, budgets) {
  const problems = [];
  const routes = new Set(measured.map((row) => row.route));
  for (const row of measured) {
    const entry = budgets[row.route];
    const kb = toKb(row.bytes);
    if (!entry) {
      problems.push(
        `${row.route} has no budget in ${BUDGET_FILE}; it loads ${formatKb(kb)} of JS first. Add ` +
          `"${row.route}": { "kB": ${kb.toFixed(1)}, "reason": "<why the web gains this route>" } ` +
          `(or run \`node scripts/bundle-budget.mjs --write\` and add the reason).`,
      );
    } else if (kb > entry.kB) {
      problems.push(
        `${row.route} loads ${formatKb(kb)} of JS first, over its budget of ${formatKb(entry.kB)} by ` +
          `${formatKb(Math.round((kb - entry.kB) * 10) / 10)}; ${chunksHint(row.route)}. Load the heavy part ` +
          `later (next/dynamic, a smaller import), or raise its "kB" in ${BUDGET_FILE} and say why in its "reason".`,
      );
    }
  }
  for (const route of Object.keys(budgets)) {
    if (!routes.has(route)) {
      problems.push(`${BUDGET_FILE} lists ${route}, which the build does not have: delete its entry.`);
    }
  }
  return problems;
}

/**
 * Problems of the budgets compared with the base ref's: an entry that is new or higher needs a reason, and a raise
 * needs a reason other than the one the base already had.
 */
export function checkRatchet(budgets, baseBudgets, baseRef) {
  const problems = [];
  for (const [route, entry] of Object.entries(budgets)) {
    const base = baseBudgets[route];
    const reason = entry.reason?.trim() ?? "";
    if (!base) {
      if (!reason) {
        problems.push(
          `${BUDGET_FILE} adds ${route} (${formatKb(entry.kB)}), which ${baseRef} does not have, without a ` +
            `"reason": say in the entry why the web gains this route.`,
        );
      }
    } else if (entry.kB > base.kB && (!reason || reason === (base.reason?.trim() ?? ""))) {
      problems.push(
        `${BUDGET_FILE} raises ${route} from ${formatKb(base.kB)} on ${baseRef} to ${formatKb(entry.kB)} ` +
          `without a new "reason": say in the entry what the extra JS buys, or keep the budget and ` +
          `${chunksHint(route)}.`,
      );
    }
  }
  return problems;
}

/**
 * The base ref's budgets: null when the ref has no budget file (the file being checked is the first one), and throws
 * when the ref itself cannot be read.
 */
export function readBaseBudgets(webDir, baseRef) {
  const git = (args) => execFileSync("git", args, { cwd: webDir, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  try {
    git(["rev-parse", "--verify", "--quiet", `${baseRef}^{commit}`]);
  } catch {
    throw new Error(`cannot read the base ref ${baseRef}: fetch it (git fetch origin main) or pass --base <ref>`);
  }
  let text;
  try {
    text = git(["show", `${baseRef}:./${BUDGET_FILE}`]);
  } catch {
    return null;
  }
  return parseBudgets(text, `${BUDGET_FILE} on ${baseRef}`);
}

export function formatTable(measured, budgets) {
  const header = ["Route", "First Load JS", "Budget", "Headroom"];
  const lines = measured.map((row) => {
    const kb = toKb(row.bytes);
    const budget = budgets[row.route]?.kB;
    return [
      row.route,
      formatKb(kb),
      budget === undefined ? "none" : formatKb(budget),
      budget === undefined ? "" : formatKb(Math.round((budget - kb) * 10) / 10),
    ];
  });
  const widths = header.map((_, i) => Math.max(header[i].length, ...lines.map((line) => line[i].length)));
  return [header, ...lines]
    .map((line) => line.map((cell, i) => (i === 0 ? cell.padEnd(widths[i]) : cell.padStart(widths[i]))).join("  "))
    .join("\n");
}

function formatChunks(row) {
  const lines = row.chunks.map((chunk) => `${formatKb(toKb(chunk.bytes)).padStart(10)}  ${chunk.file}`);
  return [
    `${row.route}: ${formatKb(toKb(row.bytes))} of First Load JS (gzip) in ${row.chunks.length} chunks`,
    ...lines,
    "The modules inside each chunk: `pnpm exec next experimental-analyze` (Turbopack's bundle analyzer).",
  ].join("\n");
}

function writeBudgets(webDir, measured, budgets) {
  const routes = {};
  for (const row of [...measured].sort((a, b) => a.route.localeCompare(b.route))) {
    const reason = budgets[row.route]?.reason;
    routes[row.route] = reason ? { kB: toKb(row.bytes), reason } : { kB: toKb(row.bytes) };
  }
  writeFileSync(path.join(webDir, BUDGET_FILE), `${JSON.stringify({ $comment: BUDGET_COMMENT, routes }, null, 2)}\n`);
}

function parseArgs(argv) {
  const args = { base: undefined, chunks: undefined, write: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--write") args.write = true;
    else if (arg === "--base" || arg === "--chunks") {
      if (i + 1 >= argv.length) throw new Error(`${arg} needs a value`);
      args[arg.slice(2)] = argv[++i];
    } else throw new Error(`unknown argument ${arg}`);
  }
  return args;
}

/** Runs the command line in `webDir` and returns the exit code; writes to `out` and `err`. */
export function main(argv, { webDir, out = console.log, err = console.error } = {}) {
  let args;
  try {
    args = parseArgs(argv);
  } catch (error) {
    err(`bundle-budget: ${error.message}`);
    return 2;
  }
  try {
    const measured = measureRoutes(webDir);
    if (args.chunks !== undefined) {
      const row = measured.find((candidate) => candidate.route === args.chunks);
      if (!row) {
        err(`bundle-budget: no route ${args.chunks} in the build; routes: ${measured.map((r) => r.route).join(", ")}`);
        return 2;
      }
      out(formatChunks(row));
      return 0;
    }
    const budgetPath = path.join(webDir, BUDGET_FILE);
    const budgets = existsSync(budgetPath) ? parseBudgets(readFileSync(budgetPath, "utf8"), BUDGET_FILE) : {};
    if (args.write) {
      writeBudgets(webDir, measured, budgets);
      out(`bundle-budget: wrote ${BUDGET_FILE} with ${measured.length} routes`);
      return 0;
    }
    out(formatTable(measured, budgets));
    const problems = checkBudgets(measured, budgets);
    const baseRef = args.base ?? DEFAULT_BASE;
    let baseBudgets;
    try {
      baseBudgets = readBaseBudgets(webDir, baseRef);
    } catch (error) {
      if (args.base !== undefined) throw error;
      out(`bundle-budget: ${error.message}; the budgets were not compared with a base`);
      baseBudgets = undefined;
    }
    if (baseBudgets === null) out(`bundle-budget: ${baseRef} has no ${BUDGET_FILE}; this file is the first budget`);
    else if (baseBudgets) problems.push(...checkRatchet(budgets, baseBudgets, baseRef));
    if (problems.length) {
      err("");
      for (const problem of problems) err(`bundle-budget: ${problem}`);
      return 1;
    }
    out(`bundle-budget: ${measured.length} routes within their budgets`);
    return 0;
  } catch (error) {
    err(`bundle-budget: ${error.message}`);
    return 2;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const webDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  process.exitCode = main(process.argv.slice(2), { webDir });
}
