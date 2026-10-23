import type { MetadataRoute } from "next";

import { PRODUCT_PATH } from "@/lib/config";

/**
 * Crawlers may read the product page (at `/` and PRODUCT_PATH) and what it loads: its images, scripts, styles and
 * fonts, and the icons. Everything else is the hub's and stays out, as the noindex of every other page says too.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: ["/$", `${PRODUCT_PATH}$`, "/product/", "/_next/static/", "/icon.svg", "/apple-icon.png"],
        disallow: "/",
      },
    ],
  };
}
