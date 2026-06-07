"use client";

import { useQuery } from "@tanstack/react-query";
import { rfpApi } from "@/lib/api";
import { isProcessing } from "@/lib/utils";
import type { ThreadStatusResponse } from "@/types/api";

/**
 * Polls GET /rfp/threads/{threadId}/status with exponential backoff.
 *
 * Polling rules
 * ─────────────
 * - Active states (parsing, retrieving, drafting, compiling): poll every 3s.
 * - Terminal states (complete, failed, awaiting_review): stop polling.
 * - Query is disabled when tenantId or threadId is absent.
 */
export function useThreadStatus(tenantId: string | undefined, threadId: string | undefined) {
  return useQuery<ThreadStatusResponse>({
    queryKey: ["thread-status", threadId],
    queryFn:  () => rfpApi.getStatus(tenantId!, threadId!),
    enabled:  !!tenantId && !!threadId,
    refetchInterval(query) {
      const status = query.state.data?.workflow_status;
      if (!status) return 3_000;
      return isProcessing(status) ? 3_000 : false;
    },
  });
}
