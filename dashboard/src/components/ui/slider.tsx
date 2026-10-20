import * as React from "react";
import { cn } from "@/lib/utils";

export function Slider({ className, style, ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  const min = Number(props.min ?? 0);
  const max = Number(props.max ?? 100);
  const value = Number(props.value ?? min);
  const pct = max > min ? Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100)) : 0;

  return (
    <input
      type="range"
      className={cn(
        "h-2 w-full cursor-pointer appearance-none rounded-full",
        "[&::-webkit-slider-thumb]:size-4 [&::-webkit-slider-thumb]:appearance-none",
        "[&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-primary",
        "[&::-moz-range-thumb]:size-4 [&::-moz-range-thumb]:rounded-full",
        "[&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-primary",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      style={{
        background: `linear-gradient(to right, var(--brand-mint-soft) 0%, var(--brand-mint-soft) ${pct}%, var(--muted) ${pct}%, var(--muted) 100%)`,
        ...style,
      }}
      {...props}
    />
  );
}
