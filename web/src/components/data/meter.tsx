import { cn } from "@/lib/utils";

/** The fill of a meter by how near its end the value is: the main measure, then the attention and danger marks. */
const FILL = {
  normal: "bg-chart-1",
  warning: "bg-attention-solid",
  danger: "bg-danger-solid",
} as const;

export type MeterTone = keyof typeof FILL;

/** The text colour that goes with a meter's tone, for the figure written beside it. */
export const METER_TEXT: Record<MeterTone, string> = {
  normal: "text-foreground",
  warning: "text-attention",
  danger: "text-danger",
};

/**
 * The kit's meter: how much of a whole is used, 0 to 100, as a bar 8 px high inside a `border-control` edge on
 * `surface-sunken`, filled in `chart-1`, `attention-solid` (warning) or `danger-solid` (danger). Each fill keeps 3:1 on
 * the ground in both themes. It is a `progressbar` with its value, named by `label` or by the element `labelledBy`
 * names; the figure is also written beside it by the caller, so the tone is never the only cue.
 */
export function Meter({
  value,
  tone = "normal",
  label,
  labelledBy,
  valueText,
  className,
}: {
  value: number;
  tone?: MeterTone;
  label?: string;
  labelledBy?: string;
  valueText?: string;
  className?: string;
}) {
  const shown = Math.round(Math.min(Math.max(value, 0), 100));
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={shown}
      aria-valuetext={valueText}
      aria-label={label}
      aria-labelledby={labelledBy}
      data-tone={tone}
      className={cn("h-2 w-full overflow-hidden rounded-full border border-input bg-muted", className)}
    >
      <div className={cn("h-full rounded-full", FILL[tone])} style={{ width: `${shown}%` }} />
    </div>
  );
}
