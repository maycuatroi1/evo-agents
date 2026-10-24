import type { Tone } from "@/lib/kg/graph";

/** The colours and font the graph canvas draws with, read from the design tokens of the theme in use. */
export type Theme = {
  tones: Record<Tone, string>;
  foreground: string;
  muted: string;
  surface: string;
  brand: string;
  ring: string;
  font: string;
};

const FALLBACK: Theme = {
  tones: { 1: "#2f4bd8", 2: "#bc7800", 3: "#0f8c80", 4: "#7c4de8", 5: "#85858d" },
  foreground: "#09090b",
  muted: "#52525b",
  surface: "#ffffff",
  brand: "#2f4bd8",
  ring: "#2f4bd8",
  font: "sans-serif",
};

export function readTheme(): Theme {
  const root = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) => root.getPropertyValue(name).trim() || fallback;
  return {
    tones: {
      1: token("--chart-1", FALLBACK.tones[1]),
      2: token("--chart-2", FALLBACK.tones[2]),
      3: token("--chart-3", FALLBACK.tones[3]),
      4: token("--chart-4", FALLBACK.tones[4]),
      5: token("--chart-5", FALLBACK.tones[5]),
    },
    foreground: token("--foreground", FALLBACK.foreground),
    muted: token("--muted-foreground", FALLBACK.muted),
    surface: token("--background", FALLBACK.surface),
    brand: token("--brand", FALLBACK.brand),
    ring: token("--ring", FALLBACK.ring),
    font: getComputedStyle(document.body).fontFamily || FALLBACK.font,
  };
}
