import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What a hub admin sets for the whole hub (evo_agents.hub.server.branding and .features): the branding the web
 * applies (the product's name, a logo, a favicon, the colours of four tokens of web/DESIGN.md) and which features
 * are on, for the hub and for each project. With nothing set the web is what it was without either: its own name,
 * the mark, the favicon and the colours of globals.css, and every menu entry.
 */
type Schemas = components["schemas"];
export type Branding = Schemas["Branding"];
export type BrandingColors = Schemas["BrandingColors"];
export type Features = Schemas["Features"];

/** The features core declares (evo_agents.hub.features.FEATURES), in its order. */
export const FEATURE_IDS = ["chats", "curator", "kg", "monitor", "market", "seller", "canvas", "srs", "tasks"] as const;
export type FeatureId = (typeof FEATURE_IDS)[number];

/** The tokens a hub may colour (evo_agents.hub.branding.TOKENS), each for the light and the dark theme. */
export const BRAND_TOKENS = ["brand", "brand-hover", "brand-soft", "on-brand"] as const;
export type BrandToken = (typeof BRAND_TOKENS)[number];
export const THEMES = ["light", "dark"] as const;
export type Theme = (typeof THEMES)[number];

export const settingsKeys = {
  branding: ["hub", "branding"] as const,
  features: ["hub", "features"] as const,
};

/** GET /v1/branding: needs no credential, so the sign-in page reads it too. */
export const brandingQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: settingsKeys.branding,
    queryFn: ({ signal }) => call(api().GET("/v1/branding", { signal })),
  });

/** GET /v1/features: whether each feature is on for the hub, and what each project the visitor sees sets. */
export const featuresQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: settingsKeys.features,
    queryFn: ({ signal }) => call(api().GET("/v1/features", { signal })),
  });

/**
 * Whether feature `id` is on, in `project` when given: what the project's layer sets when the feature may be set per
 * project, else what the hub's says. On while the answer is unknown (not read yet, or the read failed) and for an
 * item that names no feature, so a hub with nothing set shows every entry it showed before.
 */
export function featureOn(features: Features | undefined, id: FeatureId | undefined, project?: string | null): boolean {
  if (!features || !id) return true;
  const state = features.features.find((feature) => feature.id === id);
  if (project && state?.layers.includes("project")) {
    const own = features.projects.find((entry) => entry.project === project)?.values[id];
    if (own !== undefined) return own;
  }
  return state?.enabled ?? true;
}

const HEX = /^#[0-9a-f]{6}$/i;

function themeRule(selector: string, tokens: Record<string, string> | undefined): string {
  const declarations = BRAND_TOKENS.flatMap((token) => {
    const color = tokens?.[token];
    return color && HEX.test(color) ? [`--${token}:${color.toLowerCase()}`] : [];
  });
  return declarations.length ? `${selector}{${declarations.join(";")}}` : "";
}

/**
 * The style rules that put the branding's colours over the tokens of globals.css: one rule per theme that sets any,
 * only for the tokens of BRAND_TOKENS and only for #rrggbb values (the hub checks them too), so nothing else can be
 * written into the page. The selectors outrank `:root` and `.dark` of globals.css, and the aliases there
 * (`--running`, `--ring`, `--chart-1`) follow `--brand`. Empty when the branding sets no colour.
 */
export function brandingCss(colors: BrandingColors | undefined | null): string {
  return themeRule("html:root:not(.dark)", colors?.light) + themeRule("html.dark", colors?.dark);
}
