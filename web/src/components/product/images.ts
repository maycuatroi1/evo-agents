/**
 * The screenshots the product page shows, cut by scripts/product-images.mjs from the demo's shots in docs/images into
 * public/product/: a wide one for a window 768 px wide or more, a phone one below, each light and dark.
 */
export type ProductImage = "monitor" | "plan-run" | "run-spool" | "kg-search";
export type ImageSize = "wide" | "phone";
export type Scheme = "light" | "dark";

export const IMAGE_SIZES: Record<ImageSize, { width: number; height: number }> = {
  wide: { width: 1200, height: 900 },
  phone: { width: 375, height: 600 },
};

export function imagePath(image: ProductImage, size: ImageSize, scheme: Scheme): string {
  return `/product/${image}-${size}-${scheme}.png`;
}

/** The OpenGraph image, the same origin as the page: the CSP allows no other. */
export const OG_IMAGE = { url: "/product/og.png", width: 1200, height: 630 } as const;
