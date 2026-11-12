"use client";

import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

import { FALLBACK_THEME, readTheme, type Theme } from "@/lib/kg/canvas-theme";

function isDark(): boolean {
  return typeof document !== "undefined" && document.documentElement.classList.contains("dark");
}

/**
 * The design tokens the graph canvases draw with, read again once the page's theme changes (next-themes switches the
 * root's class, then the tokens follow on the next frame). Before the browser can read them, the light theme's.
 */
export function useCanvasTheme(): Theme {
  const { resolvedTheme } = useTheme();
  const [theme, setTheme] = useState<Theme>(() => (typeof document === "undefined" ? FALLBACK_THEME : readTheme(isDark())));
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const next = readTheme(resolvedTheme === undefined ? isDark() : resolvedTheme === "dark");
      setTheme((now) => (JSON.stringify(now) === JSON.stringify(next) ? now : next));
    });
    return () => cancelAnimationFrame(frame);
  }, [resolvedTheme]);
  return theme;
}
