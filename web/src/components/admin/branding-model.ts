import { BRAND_TOKENS, type BrandToken, THEMES, type Theme } from "@/lib/hub-settings";

/**
 * The rules of evo_agents.hub.branding, so the Branding page says what the hub will refuse before it is sent: the
 * colours of web/DESIGN.md the checks read, the pairs and the ratio each needs (WCAG 2.2 AA: text 4.5:1 on each ground
 * it is drawn on, the selected nav icon 3:1), and the product's name. The hub checks them again on every write.
 */

export const MAX_NAME = 60;
export const HEX = /^#[0-9a-fA-F]{6}$/;
const CONTROL = /[\u0000-\u001f\u007f]/; // the control characters a name may not hold

type Ground = "canvas" | "surface" | "surface-selected";
export type PaletteToken = BrandToken | Ground;
/** The grounds CHECKS measures a token on. */
export type GroundToken = Ground | "brand-soft" | "brand";

/** As web/src/app/globals.css sets them; evo_agents.hub.branding.DEFAULTS holds the same. */
export const DEFAULTS: Record<Theme, Record<PaletteToken, string>> = {
  light: {
    canvas: "#f5f6f8",
    surface: "#ffffff",
    "surface-selected": "#e9edfd",
    brand: "#2f4bd8",
    "brand-hover": "#263fba",
    "brand-soft": "#ebeefd",
    "on-brand": "#ffffff",
  },
  dark: {
    canvas: "#0a0c10",
    surface: "#111419",
    "surface-selected": "#1a2246",
    brand: "#8c9dff",
    "brand-hover": "#a3b0ff",
    "brand-soft": "#19204a",
    "on-brand": "#0a0c10",
  },
};

/** The ink of each theme (`--fg` of globals.css), which the preview writes its neutral text in. */
export const INK: Record<Theme, string> = { light: "#0e1116", dark: "#e9ecf1" };

export type CheckId = "linkOnCard" | "linkOnPage" | "pill" | "navIcon" | "linkHover" | "onBrand";

/** The pairs of evo_agents.hub.branding.CHECKS, in its order; `id` names the use in the page's messages. */
export const CHECKS: readonly { id: CheckId; front: BrandToken; ground: GroundToken; least: number }[] = [
  { id: "linkOnCard", front: "brand", ground: "surface", least: 4.5 },
  { id: "linkOnPage", front: "brand", ground: "canvas", least: 4.5 },
  { id: "pill", front: "brand", ground: "brand-soft", least: 4.5 },
  { id: "navIcon", front: "brand", ground: "surface-selected", least: 3 },
  { id: "linkHover", front: "brand-hover", ground: "surface", least: 4.5 },
  { id: "onBrand", front: "on-brand", ground: "brand", least: 4.5 },
];

export type Colors = Record<Theme, Partial<Record<BrandToken, string>>>;

function channel(value: number): number {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** The relative luminance of `#rrggbb` (WCAG 2.2). */
export function luminance(color: string): number {
  const [red, green, blue] = [1, 3, 5].map((at) => parseInt(color.slice(at, at + 2), 16));
  return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue);
}

/** The contrast ratio of two `#rrggbb` colours, from 1 to 21. */
export function contrast(first: string, second: string): number {
  const [lighter, darker] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

/** A ratio as the page shows it: rounded down to two decimals, so a pair just under its least never reads as passing. */
export function shownRatio(ratio: number): string {
  return (Math.floor(ratio * 100) / 100).toFixed(2);
}

/** The tokens in `theme`: the defaults, with the valid colours `colors` sets over them. */
export function palette(theme: Theme, colors: Colors): Record<PaletteToken, string> {
  const set = Object.entries(colors[theme] ?? {}).filter(([, value]) => typeof value === "string" && HEX.test(value));
  return { ...DEFAULTS[theme], ...Object.fromEntries(set.map(([token, value]) => [token, value!.toLowerCase()])) };
}

export type CheckResult = {
  id: CheckId;
  theme: Theme;
  front: BrandToken;
  ground: GroundToken;
  frontColor: string;
  groundColor: string;
  least: number;
  ratio: number;
  passes: boolean;
};

/** Every pair of CHECKS in both themes, with its ratio and whether it reaches its least. */
export function checkColors(colors: Colors): CheckResult[] {
  return THEMES.flatMap((theme) => {
    const tokens = palette(theme, colors);
    return CHECKS.map(({ id, front, ground, least }) => {
      const ratio = contrast(tokens[front], tokens[ground]);
      return { id, theme, front, ground, frontColor: tokens[front], groundColor: tokens[ground], least, ratio, passes: ratio >= least };
    });
  });
}

/** The colours a branding's values set, by theme and token, each in lower case. */
export function colorsOf(values: Record<string, unknown>): Colors {
  const held = (values.colors ?? {}) as Partial<Record<Theme, Record<string, string>>>;
  const pick = (theme: Theme) =>
    Object.fromEntries(BRAND_TOKENS.flatMap((token) => (held[theme]?.[token] ? [[token, held[theme]![token].toLowerCase()]] : [])));
  return { light: pick("light"), dark: pick("dark") };
}

/** The colours as the hub stores them: only the tokens set, only the themes that set one. */
export function storedColors(colors: Colors): Partial<Colors> {
  const stored: Partial<Colors> = {};
  for (const theme of THEMES) {
    const set = Object.entries(colors[theme]).filter(([, value]) => value);
    if (set.length) stored[theme] = Object.fromEntries(set.map(([token, value]) => [token, value!.toLowerCase()]));
  }
  return stored;
}

export type NameProblem = "required" | "invalid" | null;

/** What the hub would say of a product's name, as typed. */
export function nameProblem(name: string): NameProblem {
  if (!name.trim()) return "required";
  if (name.length > MAX_NAME || CONTROL.test(name) || name !== name.trim()) return "invalid";
  return null;
}
