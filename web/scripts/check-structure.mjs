// The checks of eslint/structure.mjs that need the whole tree or git, run by `pnpm lint` after ESLint:
//
// - no import cycle among the files of src (type-only imports count);
// - every entry of eslint/structure-baseline.json names a file that exists, and a length over the budget;
// - the baseline only shrinks: against main, no entry is added and no frozen length is raised.
//
// Main is the merge base of HEAD and origin/main (or $STRUCTURE_BASE_REF). In CI, where the checkout of a pull request
// has no origin/main, the script fetches it first. `--base <file>` compares against that baseline file instead of git.
// A main without a baseline file, as before these rules landed, skips the comparison.
import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { parseArgs } from "node:util";

import ts from "typescript";

import {
  BASELINE_FILE,
  compareBaselines,
  parseBaseline,
  readBaseline,
  resolveImport,
  staleBaselineEntries,
  webDir,
} from "../eslint/structure.mjs";

function sourceFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(full);
    return /\.tsx?$/.test(entry.name) ? [full] : [];
  });
}

function importGraph() {
  const graph = new Map();
  for (const file of sourceFiles(path.join(webDir, "src"))) {
    const from = path.relative(webDir, file).split(path.sep).join("/");
    const { importedFiles } = ts.preProcessFile(readFileSync(file, "utf8"), true, true);
    const targets = importedFiles.map((ref) => resolveImport(file, ref.fileName)).filter(Boolean);
    graph.set(from, new Set(targets));
  }
  return graph;
}

// Strongly connected components with more than one file (or a file importing itself), by Tarjan's algorithm.
function cycles(graph) {
  const index = new Map();
  const low = new Map();
  const stack = [];
  const onStack = new Set();
  const found = [];
  let counter = 0;
  const visit = (node) => {
    index.set(node, counter);
    low.set(node, counter);
    counter += 1;
    stack.push(node);
    onStack.add(node);
    for (const next of graph.get(node) ?? []) {
      if (!graph.has(next)) continue;
      if (!index.has(next)) {
        visit(next);
        low.set(node, Math.min(low.get(node), low.get(next)));
      } else if (onStack.has(next)) {
        low.set(node, Math.min(low.get(node), index.get(next)));
      }
    }
    if (low.get(node) !== index.get(node)) return;
    const component = [];
    let member;
    do {
      member = stack.pop();
      onStack.delete(member);
      component.push(member);
    } while (member !== node);
    if (component.length > 1 || graph.get(node).has(node)) found.push(component.sort());
  };
  for (const node of [...graph.keys()].sort()) if (!index.has(node)) visit(node);
  return found;
}

// One closed path through a component, for the message: the shortest way from its first file back to itself.
function cyclePath(graph, component) {
  const members = new Set(component);
  const start = component[0];
  const previous = new Map();
  const queue = [start];
  while (queue.length > 0) {
    const node = queue.shift();
    for (const next of graph.get(node)) {
      if (!members.has(next)) continue;
      if (next === start) {
        const route = [node];
        while (route[0] !== start) route.unshift(previous.get(route[0]));
        return [...route, start];
      }
      if (!previous.has(next)) {
        previous.set(next, node);
        queue.push(next);
      }
    }
  }
  return component;
}

function git(...args) {
  const result = spawnSync("git", args, { cwd: webDir, encoding: "utf8" });
  return result.status === 0 ? result.stdout.trim() : null;
}

// The baseline on main, or a string saying why there is none to compare with.
function mainBaseline() {
  const ref = process.env.STRUCTURE_BASE_REF || "origin/main";
  const commitOf = () => git("rev-parse", "--verify", "--quiet", `${ref}^{commit}`);
  let commit = commitOf();
  if (!commit && process.env.CI && ref === "origin/main") {
    git("fetch", "--quiet", "--no-tags", "--depth=1", "origin", "+refs/heads/main:refs/remotes/origin/main");
    commit = commitOf();
  }
  if (!commit) return { missing: `${ref} is not available` };
  const base = git("merge-base", "HEAD", commit) ?? commit;
  const label = `${ref} (${base.slice(0, 10)})`;
  const text = git("show", `${base}:./${BASELINE_FILE}`);
  if (text === null) return { label, baseline: null };
  return { label, baseline: parseBaseline(text) };
}

const { values } = parseArgs({ options: { base: { type: "string" } } });
const problems = [];

const graph = importGraph();
for (const component of cycles(graph)) {
  problems.push(
    `import cycle: ${cyclePath(graph, component).join(" -> ")}. web/src allows no import cycle, type-only imports ` +
      "included: move what these files share into a module that imports none of them, usually a layer down (a " +
      "shared part or lib).",
  );
}

const head = readBaseline();
problems.push(...staleBaselineEntries(head).map((problem) => `${BASELINE_FILE}: ${problem}`));

const main = values.base
  ? { label: values.base, baseline: readBaseline(path.resolve(values.base)) }
  : mainBaseline();
if (main.missing) {
  if (process.env.CI) {
    problems.push(`${BASELINE_FILE}: cannot compare with main: ${main.missing}.`);
  } else {
    process.stderr.write(`check-structure: ${main.missing}; skipped the comparison of the baseline with main.\n`);
  }
} else if (main.baseline) {
  problems.push(...compareBaselines(main.baseline, head).map((problem) => `${BASELINE_FILE}: ${problem}`));
}

if (problems.length > 0) {
  for (const problem of problems) process.stderr.write(`${problem}\n`);
  process.exit(1);
}

const imports = Object.values(head.imports).reduce((sum, targets) => sum + targets.length, 0);
const against = main.missing
  ? "not compared with main"
  : main.baseline
    ? `nothing added or raised against ${main.label}`
    : `no baseline on ${main.label} yet`;
process.stdout.write(
  `check-structure: no import cycle among ${graph.size} files of src; baseline of ` +
    `${Object.keys(head.maxLines).length} long files and ${imports} imports, ${against}.\n`,
);
