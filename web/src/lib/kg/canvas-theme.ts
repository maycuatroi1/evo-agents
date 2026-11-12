import type { Tone } from "./graph";

/**
 * The colours and font the graph canvases draw with, read from the design tokens of the theme in use, so a canvas
 * follows the page's light or dark theme. Every canvas of the knowledge graph (the explorer's scopes, a node's page,
 * the Entities view) reads the same tokens.
 */
export type Theme = {
  tones: Record<Tone, string>;
  foreground: string;
  muted: string;
  surface: string;
  brand: string;
  ring: string;
  font: string;
  /** Hairlines and the line of an edge at rest. */
  border: string;
  sunken: string;
  subtle: string;
  dark: boolean;
};

export const FALLBACK_THEME: Theme = {
  tones: { 1: "#2f4bd8", 2: "#bc7800", 3: "#0f8c80", 4: "#7c4de8", 5: "#85858d" },
  foreground: "#09090b",
  muted: "#52525b",
  surface: "#ffffff",
  brand: "#2f4bd8",
  ring: "#2f4bd8",
  font: "sans-serif",
  border: "#e4e4e7",
  sunken: "#f4f4f5",
  subtle: "#6b6b73",
  dark: false,
};

export function readTheme(dark: boolean): Theme {
  const root = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) => root.getPropertyValue(name).trim() || fallback;
  const fallback = FALLBACK_THEME;
  return {
    tones: {
      1: token("--chart-1", fallback.tones[1]),
      2: token("--chart-2", fallback.tones[2]),
      3: token("--chart-3", fallback.tones[3]),
      4: token("--chart-4", fallback.tones[4]),
      5: token("--chart-5", fallback.tones[5]),
    },
    foreground: token("--foreground", fallback.foreground),
    muted: token("--muted-foreground", fallback.muted),
    surface: token("--background", fallback.surface),
    brand: token("--brand", fallback.brand),
    ring: token("--ring", fallback.ring),
    font: getComputedStyle(document.body).fontFamily || fallback.font,
    border: token("--border", fallback.border),
    sunken: token("--muted", fallback.sunken),
    subtle: token("--fg-subtle", fallback.subtle),
    dark,
  };
}
