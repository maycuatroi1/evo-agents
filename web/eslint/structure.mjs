// Size and dependency-direction rules for web/src, run by `pnpm lint`.
//
// Layers, top to bottom: the routes under src/app (and the files at the root of src, such as proxy.ts); the feature
// folders under src/components; the shared parts (components/ui, shell, states, data, feedback, live, status and the
// files at the root of components); then lib, hooks and i18n. A file imports its own folder and the layers below it,
// never a layer above it nor another feature folder. src/test holds helpers that only test files import. A file has at
// most 400 lines; test files, src/test and the generated schema.d.ts are exempt.
//
// structure-baseline.json, next to this file, lists what was already over on the day these rules landed: the files
// longer than 400 lines, each frozen at its length then, and the imports against the direction above. The list may
// only shrink: structure/baseline flags an entry the code no longer needs, and scripts/check-structure.mjs, which
// `pnpm lint` runs after ESLint, fails on an entry added or raised against main. That script also checks what a
// per-file rule cannot see: no import cycle among the files of src.
import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import boundaries from "eslint-plugin-boundaries";
import { builtinRules } from "eslint/use-at-your-own-risk";

const MAX_LINES = 400;
export const BASELINE_FILE = "eslint/structure-baseline.json";
const SHARED_PARTS = ["ui", "shell", "states", "data", "feedback", "live", "status"];

export const webDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SOURCES = ["src/**/*.{ts,tsx}"];
const TESTS = ["src/**/*.test.{ts,tsx}", "src/test/**"];
const GENERATED = ["src/lib/api/schema.d.ts"];
const NO_GROWTH = "Do not add it to eslint/structure-baseline.json: that list may only shrink.";

/** The baseline in a JSON text: `maxLines` maps a file to its frozen length, `imports` a file to what it may import. */
export function parseBaseline(text) {
  const raw = JSON.parse(text);
  return { maxLines: raw.maxLines ?? {}, imports: raw.imports ?? {} };
}

/** Reads the baseline; a missing file reads as an empty one. */
export function readBaseline(file = path.join(webDir, BASELINE_FILE)) {
  try {
    return parseBaseline(readFileSync(file, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return { maxLines: {}, imports: {} };
    throw error;
  }
}

const isFile = (file) => existsSync(file) && statSync(file).isFile();

/** Escapes a literal path for the glob matchers of ESLint (minimatch) and of the boundaries plugin (micromatch). */
function literalGlob(file) {
  return file.replace(/[\\*?[\]{}()!+@]/g, "\\$&");
}

// Lines as max-lines counts them: every line, without the empty one after a final line break.
function lineCount(sourceCode) {
  const { lines } = sourceCode;
  return lines.length > 1 && lines.at(-1) === "" ? lines.length - 1 : lines.length;
}

/**
 * Resolves an import specifier of `fromFile` (an absolute path) the way tsconfig.json does, `@/` being src/, to a path
 * relative to web/; null for a package.
 */
export function resolveImport(fromFile, specifier) {
  let base;
  if (specifier.startsWith("@/")) base = path.join(webDir, "src", specifier.slice(2));
  else if (specifier.startsWith(".")) base = path.resolve(path.dirname(fromFile), specifier);
  else return null;
  const candidates = ["", ".ts", ".tsx", "/index.ts", "/index.tsx"].map((suffix) => base + suffix);
  const found = candidates.find(isFile);
  return path.relative(webDir, found ?? base).split(path.sep).join("/");
}

const coreMaxLines = builtinRules.get("max-lines");

// The core max-lines rule with a message that says how to get under the budget.
const maxLinesRule = {
  ...coreMaxLines,
  meta: {
    ...coreMaxLines.meta,
    messages: {
      exceed:
        "File has {{actual}} lines, over its budget of {{max}}. Split it: move a self-contained part (a " +
        "subcomponent, a hook, a pure model or format function) into its own module beside it and import it back. " +
        `The budget is ${MAX_LINES} lines; a file listed in eslint/structure-baseline.json keeps the length it had ` +
        "when the list was made, and that number may only go down.",
    },
  },
};

// Flags a baseline entry the file no longer needs: an import it dropped, or a frozen length above its length now.
const baselineRule = {
  meta: {
    type: "problem",
    docs: { description: "Keep eslint/structure-baseline.json no larger than the code needs" },
    schema: [
      {
        type: "object",
        properties: {
          imports: { type: "array", items: { type: "string" } },
          maxLines: { type: "integer" },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      importGone:
        "eslint/structure-baseline.json lets this file import {{target}}, which it no longer does. Delete that entry " +
        "so the import cannot come back.",
      shorter:
        "This file has {{actual}} lines, under the {{max}} frozen for it in eslint/structure-baseline.json. Lower " +
        `its entry to {{actual}}, or delete the entry once the file has ${MAX_LINES} lines or fewer, so it cannot ` +
        "grow back.",
    },
  },
  create(context) {
    const { imports = [], maxLines } = context.options[0] ?? {};
    const seen = new Set();
    const collect = (node) => {
      if (node.source?.type !== "Literal" || typeof node.source.value !== "string") return;
      const target = resolveImport(context.filename, node.source.value);
      if (target) seen.add(target);
    };
    return {
      ImportDeclaration: collect,
      ExportNamedDeclaration: collect,
      ExportAllDeclaration: collect,
      ImportExpression: collect,
      "Program:exit"(program) {
        const loc = { line: 1, column: 0 };
        for (const target of imports) {
          if (!seen.has(target)) context.report({ node: program, loc, messageId: "importGone", data: { target } });
        }
        const actual = lineCount(context.sourceCode);
        if (maxLines !== undefined && actual < maxLines) {
          context.report({ node: program, loc, messageId: "shorter", data: { actual, max: maxLines } });
        }
      },
    };
  },
};

const structurePlugin = {
  meta: { name: "structure" },
  rules: { "max-lines": maxLinesRule, baseline: baselineRule },
};

const elements = [
  { type: "app", pattern: "src/app", partialMatch: false },
  { type: "shared", pattern: SHARED_PARTS.map((part) => `src/components/${part}`), partialMatch: false },
  { type: "feature", pattern: "src/components/*", capture: ["feature"], partialMatch: false },
  { type: "lib", pattern: ["src/lib", "src/hooks", "src/i18n"], partialMatch: false },
  { type: "test-support", pattern: "src/test", partialMatch: false },
  // Element patterns match folders: these two take the files at the root of components and of src.
  { type: "shared", pattern: "src/components", partialMatch: false },
  { type: "app", pattern: "src", partialMatch: false },
];

const element = (...types) => ({ element: { types: { anyOf: types } } });

const policies = [
  { from: element("app"), allow: { to: element("app", "feature", "shared", "lib") } },
  { from: element("feature"), allow: { to: element("shared", "lib") } },
  { from: element("shared"), allow: { to: element("shared", "lib") } },
  { from: element("lib"), allow: { to: element("lib") } },
  { from: element("test-support"), allow: { to: element("feature", "shared", "lib") } },
  {
    from: element("feature"),
    disallow: { to: element("feature") },
    message:
      "This file in components/{{from.element.captured.feature}} imports {{to.file.path}} from another feature " +
      "folder. A feature folder may import its own files, the shared parts (components/ui, shell, states, data, " +
      "feedback, live, status and the files at the root of components) and lib, hooks or i18n. Move what both " +
      `features use down into a shared part or lib, or let the page under src/app compose the two. ${NO_GROWTH}`,
  },
  {
    from: element("shared"),
    disallow: { to: element("feature") },
    message:
      "This file in the shared part {{from.elementPath}} imports {{to.file.path}} from a feature folder, a layer " +
      "above it. A shared part may import only other shared parts and lib, hooks or i18n. Move the code it needs " +
      "down into a shared part or lib, or take it as a prop or child from the feature or page that renders this " +
      `part. ${NO_GROWTH}`,
  },
  {
    from: element("lib"),
    disallow: { to: element("feature", "shared") },
    message:
      "This file in {{from.elementPath}} imports {{to.file.path}}, from a layer above it. lib, hooks and i18n are " +
      "the bottom layer and import only each other: move the code it needs into lib, or have the caller pass it in. " +
      NO_GROWTH,
  },
  {
    from: element("feature", "shared", "lib", "test-support"),
    disallow: { to: element("app") },
    message:
      "This file imports {{to.file.path}}, a route file under src/app. Routes are the top layer and nothing imports " +
      "them: move the code into the feature folder that uses it, or into a shared part or lib.",
  },
  {
    disallow: { to: element("test-support") },
    message:
      "This file imports {{to.file.path}}: src/test holds helpers for test files, and only *.test.ts or *.test.tsx " +
      "files import it. Move what this file needs into lib.",
  },
  { from: { file: { categories: "test" } }, allow: { to: element("test-support") } },
];

const defaultMessage =
  "This file imports {{to.file.path}} against the direction of web/src: src/app, then the feature folders under " +
  "components, then the shared parts (components/ui, shell, states, data, feedback, live, status), then lib, hooks " +
  "and i18n. Import only from your own folder or a layer below it; eslint/structure.mjs describes the layers.";

/** The flat-config entries for web/src, with the exemptions the baseline grants. */
export function structureConfig(baseline = readBaseline()) {
  const baselinePolicies = Object.entries(baseline.imports).map(([file, targets]) => ({
    from: { file: { path: literalGlob(file) } },
    allow: { to: { file: { path: targets.map(literalGlob) } } },
  }));
  const files = [...new Set([...Object.keys(baseline.maxLines), ...Object.keys(baseline.imports)])].sort();
  const perFile = files.map((file) => {
    const maxLines = baseline.maxLines[file];
    const imports = baseline.imports[file] ?? [];
    return {
      files: [literalGlob(file)],
      rules: {
        ...(maxLines === undefined ? {} : { "structure/max-lines": ["error", { max: maxLines }] }),
        "structure/baseline": ["error", { imports, ...(maxLines === undefined ? {} : { maxLines }) }],
      },
    };
  });
  return [
    {
      files: SOURCES,
      plugins: { boundaries, structure: structurePlugin },
      settings: {
        "boundaries/elements": elements,
        "boundaries/files": [{ category: "test", pattern: "**/*.test.{ts,tsx}" }],
      },
      rules: {
        "boundaries/dependencies": [
          "error",
          { default: "disallow", message: defaultMessage, policies: [...policies, ...baselinePolicies] },
        ],
      },
    },
    {
      files: SOURCES,
      ignores: [...TESTS, ...GENERATED],
      rules: { "structure/max-lines": ["error", { max: MAX_LINES }] },
    },
    ...perFile,
  ];
}

/**
 * What the baseline gained against an older one (main's): an entry it did not have, or a number it raised.
 * Returns one message per problem.
 */
export function compareBaselines(base, head) {
  const problems = [];
  for (const [file, max] of Object.entries(head.maxLines)) {
    const before = base.maxLines[file];
    if (before === undefined) {
      problems.push(
        `maxLines: ${file} (${max}) is not in the baseline on main. Split the file under ${MAX_LINES} lines instead.`,
      );
    } else if (max > before) {
      problems.push(`maxLines: ${file} is frozen at ${before} on main, not ${max}. Split the file instead.`);
    }
  }
  for (const [file, targets] of Object.entries(head.imports)) {
    const before = new Set(base.imports[file] ?? []);
    for (const target of targets) {
      if (!before.has(target)) {
        problems.push(
          `imports: ${file} -> ${target} is not in the baseline on main. Remove the import instead: the ` +
            "boundaries/dependencies message for it says where the code can go.",
        );
      }
    }
  }
  return problems;
}

/** Entries that name a file that is gone, or a length the budget already allows. */
export function staleBaselineEntries(baseline, root = webDir) {
  const problems = [];
  const exists = (file) => isFile(path.join(root, file));
  for (const [file, max] of Object.entries(baseline.maxLines)) {
    if (!exists(file)) problems.push(`maxLines: ${file} does not exist. Delete its entry.`);
    else if (max <= MAX_LINES) problems.push(`maxLines: ${file} is at ${max}, within the budget. Delete its entry.`);
  }
  for (const [file, targets] of Object.entries(baseline.imports)) {
    if (!exists(file)) problems.push(`imports: ${file} does not exist. Delete its entries.`);
    for (const target of targets) {
      if (!exists(target)) problems.push(`imports: ${file} -> ${target}, which does not exist. Delete the entry.`);
    }
  }
  return problems;
}
