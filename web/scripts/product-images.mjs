// The images of the product page (src/app/about), cut from the screenshots of `pnpm screenshots` in docs/images.
//
// `node scripts/product-images.mjs` writes into public/product/, which the web serves as it is:
// - <page>-wide-<scheme>.png: the 1440 px shot without the sidebar, 1200 x 900, for a window 768 px wide or more;
// - <page>-phone-<scheme>.png: 600 px of the 375 px shot from under its top bar, for a phone;
// - og.png: the 1200 x 630 OpenGraph image, the mark over the decision of the plan-run shot, with no text of its own,
//   so it renders the same on any machine.
// Then it writes src/components/product/images.json: the sha256 of each shot it read and the files it wrote, which
// src/components/product/images.test.ts compares with docs/images, so a new set of shots without this script fails
// the tests. `pnpm screenshots` runs it after compressing the shots.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import sharp from "sharp";

const webDir = fileURLToPath(new URL("..", import.meta.url));
const shotsDir = path.join(webDir, "..", "docs", "images");
const outDir = path.join(webDir, "public", "product");
const manifestFile = path.join(webDir, "src", "components", "product", "images.json");

/** The shots the page shows, and where each phone crop starts: under the top bar, at the part the page talks about. */
export const PAGES = { monitor: 52, "plan-run": 52, "run-spool": 160, "kg-search": 212 };
const SCHEMES = ["light", "dark"];
const SIDEBAR = 240; // the 1440 px shots' sidebar, cut off: the page shows the work, not the hub's navigation
const PHONE_HEIGHT = 600;

/** The mark of src/components/brand.tsx in the ink of the light theme. */
const MARK = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="6 6 36 36" width="44" height="44"><g fill="#09090b">
<path d="M38.5 24A14.5 14.5 0 1 0 34.78 33.7L28.94 31.63A9.4 9.4 0 1 1 35.91 22.55Z"/>
<path d="M13 20.75H38.13A14.5 14.5 0 0 1 38.5 24.35H13Z"/></g></svg>`;

const OG = { width: 1200, height: 630, margin: 71, panelTop: 100, background: "#f4f4f5", border: "#e4e4e7" };
/**
 * The decision of the plan-run shot (light), from its heading to its answer button, scaled to the panel's width under
 * a strip of white: the heading starts on the row under the message box's border.
 */
const OG_SOURCE = { file: "plan-run-1440-light.png", region: { left: 252, top: 299, width: 1176, height: 569 }, pad: 18 };

function png(image) {
  return image.png({ palette: true, quality: 90, effort: 10, dither: 0, compressionLevel: 9 }).toBuffer();
}

const sha256 = (buffer) => createHash("sha256").update(buffer).digest("hex");

async function ogImage(read) {
  const panelWidth = OG.width - 2 * OG.margin;
  const panelHeight = OG.height - OG.panelTop; // the panel runs off the bottom edge, as a window would
  const radius = 10;
  const shotHeight = panelHeight - OG_SOURCE.pad;
  const shot = await sharp({ create: { width: panelWidth, height: panelHeight, channels: 3, background: "#ffffff" } })
    .composite([
      {
        input: await sharp(read(OG_SOURCE.file)).extract(OG_SOURCE.region).resize(panelWidth, shotHeight, { fit: "fill" }).toBuffer(),
        left: 0,
        top: OG_SOURCE.pad,
      },
    ])
    .png()
    .toBuffer();
  const corners = `<svg xmlns="http://www.w3.org/2000/svg" width="${panelWidth}" height="${panelHeight}">
<rect width="${panelWidth}" height="${panelHeight + 2 * radius}" rx="${radius}" fill="#fff"/></svg>`;
  const edge = `<svg xmlns="http://www.w3.org/2000/svg" width="${panelWidth}" height="${panelHeight}">
<rect x="0.5" y="0.5" width="${panelWidth - 1}" height="${panelHeight + 2 * radius}" rx="${radius}" fill="none" stroke="${OG.border}"/></svg>`;
  const panel = await sharp(shot)
    .composite([
      { input: Buffer.from(corners), blend: "dest-in" },
      { input: Buffer.from(edge), blend: "over" },
    ])
    .png()
    .toBuffer();
  const mark = await sharp(Buffer.from(MARK)).png().toBuffer();
  return png(
    sharp({ create: { width: OG.width, height: OG.height, channels: 3, background: OG.background } }).composite([
      { input: mark, left: OG.margin, top: 32 },
      { input: panel, left: OG.margin, top: OG.panelTop },
    ]),
  );
}

export async function productImages() {
  mkdirSync(outDir, { recursive: true });
  const sources = {};
  const read = (name) => {
    const buffer = readFileSync(path.join(shotsDir, name));
    sources[name] = sha256(buffer);
    return buffer;
  };
  const outputs = {};
  const write = (name, buffer) => {
    writeFileSync(path.join(outDir, name), buffer);
    outputs[name] = sha256(buffer);
  };
  for (const [page, phoneTop] of Object.entries(PAGES)) {
    for (const scheme of SCHEMES) {
      const wide = sharp(read(`${page}-1440-${scheme}.png`)).extract({ left: SIDEBAR, top: 0, width: 1440 - SIDEBAR, height: 900 });
      write(`${page}-wide-${scheme}.png`, await png(wide));
      const phone = sharp(read(`${page}-375-${scheme}.png`)).extract({ left: 0, top: phoneTop, width: 375, height: PHONE_HEIGHT });
      write(`${page}-phone-${scheme}.png`, await png(phone));
    }
  }
  write("og.png", await ogImage(read));
  const sorted = (record) => Object.fromEntries(Object.entries(record).sort(([a], [b]) => a.localeCompare(b)));
  writeFileSync(manifestFile, `${JSON.stringify({ sources: sorted(sources), outputs: sorted(outputs) }, null, 2)}\n`);
  return Object.keys(outputs).length;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const count = await productImages();
  console.log(`product images: ${count} files in ${path.relative(process.cwd(), outDir) || "."}`);
}
