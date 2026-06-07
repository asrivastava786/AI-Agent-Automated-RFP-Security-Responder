"use client";

import { cn, formatConfidence } from "@/lib/utils";

interface ConfidenceBarProps {
  value: number;   // 0.0 – 1.0
  threshold?: number;
  className?: string;
}

/**
 * Visual confidence bar.
 * - Green  ≥ threshold (default 0.92) – auto-approve zone
 * - Amber  0.60 – threshold            – borderline
 * - Red    < 0.60                      – low confidence
 */
export function ConfidenceBar({
  value,
  threshold = 0.92,
  className,
}: ConfidenceBarProps) {
  const pct = Math.round(value * 100);

  const barColour =
    value >= threshold ? "bg-green-500" :
    value >= 0.6       ? "bg-amber-500" :
                         "bg-red-500";

  const textColour =
    value >= threshold ? "text-green-700" :
    value >= 0.6       ? "text-amber-700" :
                         "text-red-700";

  return (
    <div className={cn("flex items-center gap-3", className)}>
      <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
        <div
          className={cn("h-full rounded-full transition-all", barColour)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={cn("text-xs font-semibold w-9 text-right tabular-nums", textColour)}>
        {formatConfidence(value)}
      </span>
    </div>
  );
}
