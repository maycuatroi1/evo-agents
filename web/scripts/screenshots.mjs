// `pnpm screenshots`: the images of the README and the product page, from the made-up demo of e2e/demo/story.ts.
//
// Playwright runs e2e/demo/playwright.config.ts: a stack of its own (e2e/hub_stack.py on a new database of
// EVO_HUB_TEST_DSN, the web's production build), the demo seeded through the real API, and each page written to
// docs/images/<page>-<width>-<scheme>.png at the repo's root, light and dark, 1440 and 375 px wide. Then each image is
// compressed in place: a palette of at most 256 colours, as pngquant would, without dithering (it draws stripes on the
// flat fills of the UI), kept only when it is smaller. Arguments go on to `playwright test`, so
// `pnpm screenshots --headed` watches it run.
import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import sharp from "sharp";

const webDir = fileURLToPath(new URL("..", import.meta.url));
const images = path.join(webDir, "..", "docs", "images");

const run = spawnSync("pnpm", ["exec", "playwright", "test", "--config", "e2e/demo/playwright.config.ts", ...process.argv.slice(2)], {
  cwd: webDir,
  stdio: "inherit",
});
if (run.status !== 0) process.exit(run.status ?? 1);

let before = 0;
let after = 0;
const names = readdirSync(images).filter((name) => name.endsWith(".png")).sort();
for (const name of names) {
  const file = path.join(images, name);
  const original = readFileSync(file);
  const packed = await sharp(original).png({ palette: true, quality: 90, effort: 10, dither: 0, compressionLevel: 9 }).toBuffer();
  const kept = packed.length < original.length ? packed : original;
  if (kept === packed) writeFileSync(file, packed);
  before += original.length;
  after += kept.length;
}
const kib = (bytes) => `${Math.round(bytes / 1024)} KiB`;
console.log(`screenshots: ${names.length} images in ${path.relative(process.cwd(), images) || "."}, ${kib(before)} compressed to ${kib(after)}`);
