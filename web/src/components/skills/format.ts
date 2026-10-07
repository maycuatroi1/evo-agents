/** A byte count in the unit a person reads best (binary steps, as file managers show bundle sizes). */
export type SizeUnit = "byte" | "kilobyte" | "megabyte" | "gigabyte" | "terabyte";

const UNITS: readonly SizeUnit[] = ["byte", "kilobyte", "megabyte", "gigabyte", "terabyte"];

export function sizeParts(bytes: number): { value: number; unit: SizeUnit } {
  let step = 0;
  while (step < UNITS.length - 1 && bytes >= 1024 ** (step + 1)) step += 1;
  return { value: bytes / 1024 ** step, unit: UNITS[step] };
}

/** The first characters of a SHA-256, enough to tell versions apart at a glance. */
export function shortHash(sha256: string, length = 12): string {
  return sha256.slice(0, length);
}

/** The file name the hub gives a bundle download (`<name>-v<version>.tar.gz`). */
export function bundleFileName(name: string, version: number): string {
  return `${name}-v${version}.tar.gz`;
}
