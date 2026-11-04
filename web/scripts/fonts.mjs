// The IBM Plex files the web serves from its own origin, kept in src/app/fonts so `next build` never reaches Google
// Fonts: with next/font/google every build fetched them, and a release failed whenever Google answered slowly or with
// a file URL Turbopack's font loader could not parse ("next/font/google queries have exactly one entry").
//
// `node scripts/fonts.mjs` asks the Google Fonts CSS API for the faces DESIGN.md names (section "Type"), as a browser
// that takes woff2, keeps the subsets the hub is written in (latin, latin-ext, vietnamese), downloads each file once
// and writes src/app/fonts/plex.css: an @font-face per file with its unicode-range, so a page loads only the subsets
// its text needs, then a fallback face per family with the metrics next/font gives it, so the swap moves no line, and
// the two variables globals.css reads. The files are Google's, unchanged, under the SIL OFL 1.1 (src/app/fonts/OFL.txt).
import { mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { getFallbackFontOverrideMetrics } = require("next/dist/compiled/@next/font/dist/google/get-fallback-font-override-metrics");

const webDir = fileURLToPath(new URL("..", import.meta.url));
const outDir = path.join(webDir, "src", "app", "fonts");
const CSS_API = "https://fonts.googleapis.com/css2";
// A browser that takes woff2: the API answers each client with the format it reads.
const USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
const SUBSETS = ["latin", "latin-ext", "vietnamese"];

/** The faces of DESIGN.md: the family, its weights, the variable globals.css reads, and the file names' stem. */
export const FAMILIES = [
  { family: "IBM Plex Sans", weights: [400, 500, 600], variable: "--font-plex-sans", stem: "ibm-plex-sans" },
  { family: "IBM Plex Mono", weights: [400, 500], variable: "--font-plex-mono", stem: "ibm-plex-mono" },
];

async function fetchOk(url, init) {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response;
}

/** The @font-face blocks of the API's answer: subset (from the comment before each), weight, stretch, url and range. */
export function parseFaces(css) {
  const faces = [];
  for (const match of css.matchAll(/\/\*\s*([\w-]+)\s*\*\/\s*@font-face\s*{([^}]*)}/g)) {
    const [, subset, body] = match;
    const prop = (name) => body.match(new RegExp(`${name}:\\s*([^;]+);`))?.[1].trim();
    const url = prop("src")?.match(/url\(([^)]+)\)/)?.[1];
    if (!url) throw new Error(`a face of ${subset} has no url: ${body}`);
    faces.push({ subset, weight: Number(prop("font-weight")), stretch: prop("font-stretch"), url, range: prop("unicode-range") });
  }
  return faces;
}

/**
 * One file per url: a variable family answers every weight with the same file, which becomes one face over the range
 * of its weights; a static one has a file, and a face, per weight.
 */
export function groupFaces(faces) {
  const byUrl = new Map();
  for (const face of faces.filter((f) => SUBSETS.includes(f.subset))) {
    const known = byUrl.get(face.url);
    if (known) known.weights.push(face.weight);
    else byUrl.set(face.url, { ...face, weights: [face.weight] });
  }
  return [...byUrl.values()];
}

function fontFace(family, file, face) {
  const low = Math.min(...face.weights);
  const high = Math.max(...face.weights);
  return [
    "@font-face {",
    `  font-family: "${family}";`,
    "  font-style: normal;",
    `  font-weight: ${low === high ? low : `${low} ${high}`};`,
    ...(face.stretch ? [`  font-stretch: ${face.stretch};`] : []),
    "  font-display: swap;",
    `  src: url("./${file}") format("woff2");`,
    `  unicode-range: ${face.range};`,
    "}",
  ].join("\n");
}

function fallbackFace(family) {
  const metrics = getFallbackFontOverrideMetrics(family);
  if (!metrics) throw new Error(`next/font has no fallback metrics for ${family}`);
  return [
    "@font-face {",
    `  font-family: "${family} Fallback";`,
    `  src: local("${metrics.fallbackFont}");`,
    `  ascent-override: ${metrics.ascentOverride};`,
    `  descent-override: ${metrics.descentOverride};`,
    `  line-gap-override: ${metrics.lineGapOverride};`,
    `  size-adjust: ${metrics.sizeAdjust};`,
    "}",
  ].join("\n");
}

async function main() {
  mkdirSync(outDir, { recursive: true });
  for (const name of readdirSync(outDir).filter((file) => file.endsWith(".woff2"))) rmSync(path.join(outDir, name));
  const blocks = [];
  const variables = [];
  for (const { family, weights, variable, stem } of FAMILIES) {
    const query = `family=${family.replaceAll(" ", "+")}:wght@${weights.join(";")}&display=swap`;
    const css = await (await fetchOk(`${CSS_API}?${query}`, { headers: { "user-agent": USER_AGENT } })).text();
    const files = groupFaces(parseFaces(css));
    for (const subset of SUBSETS) {
      if (!files.some((face) => face.subset === subset)) throw new Error(`${family} has no ${subset} subset`);
    }
    for (const face of files) {
      const weight = face.weights.length === weights.length ? "" : `-${face.weights.join("-")}`;
      const file = `${stem}-${face.subset}${weight}.woff2`;
      writeFileSync(path.join(outDir, file), Buffer.from(await (await fetchOk(face.url)).arrayBuffer()));
      blocks.push(fontFace(family, file, face));
      console.log(`${file} from ${face.url}`);
    }
    blocks.push(fallbackFace(family));
    variables.push(`  ${variable}: "${family}", "${family} Fallback";`);
  }
  const header = "/* Written by scripts/fonts.mjs, do not edit: IBM Plex from Google Fonts (SIL OFL 1.1, OFL.txt). */";
  writeFileSync(path.join(outDir, "plex.css"), `${header}\n\n${blocks.join("\n\n")}\n\n:root {\n${variables.join("\n")}\n}\n`);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
