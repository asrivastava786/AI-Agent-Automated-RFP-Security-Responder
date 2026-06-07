import { cn, STATUS_COLOURS, STATUS_LABELS } from "@/lib/utils";
import type { WorkflowStatus } from "@/types/api";
import { Loader2 } from "lucide-react";
import { isProcessing } from "@/lib/utils";

interface ThreadStatusBadgeProps {
  status: WorkflowStatus;
  className?: string;
}

export function ThreadStatusBadge({ status, className }: ThreadStatusBadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium",
        STATUS_COLOURS[status],
        className
      )}
    >
      {isProcessing(status) && (
        <Loader2 className="w-3 h-3 animate-spin" />
      )}
      {STATUS_LABELS[status]}
    </span>
  );
}
