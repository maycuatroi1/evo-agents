"use client";

import { useFormatter } from "next-intl";

import { byteParts } from "@/lib/outputs";

/** A byte count as a person reads it: "230 bytes", "34.5 kB", "1.2 GB". */
export function useByteSize() {
  const format = useFormatter();
  return (bytes: number) => {
    const { value, unit } = byteParts(bytes);
    return format.number(value, { style: "unit", unit, unitDisplay: unit === "byte" ? "long" : "short", maximumFractionDigits: unit === "byte" ? 0 : 1 });
  };
}
