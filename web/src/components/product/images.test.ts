import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { IMAGE_SIZES, type ImageSize, imagePath, OG_IMAGE, type ProductImage, type Scheme } from "./images";
import manifest from "./images.json";

// The page's images are cut from the demo's screenshots in docs/images by scripts/product-images.mjs: a new set of
// shots without a new cut, or a crop edited by hand, fails here.
const webDir = fileURLToPath(new URL("../../..", import.meta.url));
const shotsDir = path.join(webDir, "..", "docs", "images");
const publicDir = path.join(webDir, "public");
const sha256 = (file: string) => createHash("sha256").update(readFileSync(file)).digest("hex");

/** Width and height from a PNG's IHDR chunk. */
function pngSize(file: string): { width: number; height: number } {
  const bytes = readFileSync(file);
  expect(bytes.subarray(1, 4).toString("latin1"), file).toBe("PNG");
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

const SIZES: ImageSize[] = ["wide", "phone"];
const SCHEMES: Scheme[] = ["light", "dark"];

describe("the product page's images", () => {
  it("come from the screenshots docs/images holds now", () => {
    expect(Object.keys(manifest.sources).length).toBeGreaterThan(0);
    for (const [name, hash] of Object.entries(manifest.sources)) expect(sha256(path.join(shotsDir, name)), name).toBe(hash);
  });

  it("are the files the script wrote", () => {
    for (const [name, hash] of Object.entries(manifest.outputs)) expect(sha256(path.join(publicDir, "product", name)), name).toBe(hash);
  });

  it("have each page's crops at the size the markup gives them", () => {
    const images: ProductImage[] = ["monitor", "plan-run", "run-spool", "kg-search"];
    for (const image of images) {
      for (const size of SIZES) {
        for (const scheme of SCHEMES) {
          const file = imagePath(image, size, scheme);
          expect(Object.keys(manifest.outputs), file).toContain(path.basename(file));
          expect(pngSize(path.join(publicDir, file)), file).toEqual(IMAGE_SIZES[size]);
        }
      }
    }
  });

  it("give the OpenGraph image 1200 by 630 px on this origin", () => {
    expect(OG_IMAGE.url.startsWith("/product/")).toBe(true);
    expect(Object.keys(manifest.outputs)).toContain(path.basename(OG_IMAGE.url));
    expect(pngSize(path.join(publicDir, OG_IMAGE.url))).toEqual({ width: 1200, height: 630 });
    expect({ width: OG_IMAGE.width, height: OG_IMAGE.height }).toEqual({ width: 1200, height: 630 });
  });
});
