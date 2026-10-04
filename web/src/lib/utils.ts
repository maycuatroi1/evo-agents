export { cn } from "cn";

/** One or two letters for an avatar or a project tile. */
export function initials(name: string): string {
  const parts = name.split(/[-_\s.]+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2);
  return letters.toUpperCase();
}
