"use client";

/**
 * threads/[threadId]/review/page.tsx – Human-in-the-loop review queue.
 *
 * This is the most critical page in the application.  It is reached when
 * the LangGraph workflow hits an interrupt_before=["human_review_wait"] and
 * returns HTTP 202 from POST /ingest or POST /resume.
 *
 * UX decisions
 * ────────────
 * • Batch submission: reviewers go through all cards first, then submit
 *   once.  This maps cleanly to the POST /resume endpoint which accepts an
 *   array of decisions.
 *
 * • Optimistic lock warning: if a second reviewer opens the same thread and
 *   the first submits first, the second gets a 409 CONFLICT. We catch that
 *   and show a non-destructive error ("Thread was already resumed").
 *
 * • Progress header: shows how many cards have been decided so the reviewer
 *   knows how close they are to submitting.
 */

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useSession } from "next-auth/react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { rfpApi } from "@/lib/api";
import { ReviewItemCard, type CardDecision } from "@/components/review-item-card";
import { ThreadStatusBadge } from "@/components/thread-status-badge";
import { Loader2, Send, AlertTriangle, CheckCircle } from "lucide-react";
import type { HumanReviewDecision } from "@/types/api";

export default function ReviewPage() {
  const { threadId }  = useParams<{ threadId: string }>();
  const { data: session } = useSession();
  const router        = useRouter();
  const queryClient   = useQueryClient();

  const tenantId = session?.user?.tenantId ?? "";

  // ── Fetch review items ─────────────────────────────────────────────────────
  const { data, isLoading, error: fetchError } = useQuery({
    queryKey: ["review-items", threadId],
    queryFn:  () => rfpApi.getReviewItems(tenantId, threadId),
    enabled:  !!tenantId && !!threadId,
  });

  // ── Local decision state (lifted from cards) ────────────────────────────────
  const [decisions, setDecisions] = useState<Record<string, CardDecision>>({});

  function handleDecide(questionId: string, decision: CardDecision) {
    setDecisions((prev) => ({ ...prev, [questionId]: decision }));
  }

  const decidedCount = Object.keys(decisions).length;
  const totalCount   = data?.total_pending ?? 0;
  const allDecided   = totalCount > 0 && decidedCount === totalCount;

  // ── Submit mutation ────────────────────────────────────────────────────────
  const [submitError, setSubmitError] = useState<string | null>(null);

  const { mutate: submitDecisions, isPending: isSubmitting } = useMutation({
    mutationFn: () => {
      const payload: HumanReviewDecision[] = Object.entries(decisions).map(
        ([questionId, d]) => ({
          question_id:     questionId,
          approved:        d.type !== "reject",
          override_answer: d.type === "override" ? d.overrideText : undefined,
          reviewer_id:     session?.user.email ?? "unknown",
          review_notes:    d.notes,
        })
      );
      return rfpApi.resume(tenantId, threadId, {
        decisions: payload,
        reviewer_id: session?.user.email ?? "unknown",
      });
    },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["thread-status", threadId] });
      if (result.status === "awaiting_review") {
        // Another review wave; reload this page with fresh items
        queryClient.invalidateQueries({ queryKey: ["review-items", threadId] });
        setDecisions({});
      } else {
        router.push(`/?thread=${threadId}&status=complete`);
      }
    },
    onError: (err: Error) => {
      if (err.message.includes("409")) {
        setSubmitError("This thread was already resumed by another reviewer. Reload to see the latest state.");
      } else {
        setSubmitError(`Submission failed: ${err.message}`);
      }
    },
  });

  // ── Render ─────────────────────────────────────────────────────────────────

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="w-6 h-6 animate-spin text-indigo-600" />
      </div>
    );
  }

  if (fetchError) {
    return (
      <div className="p-8 text-center text-red-600">
        Failed to load review items: {fetchError.message}
      </div>
    );
  }

  const items = data?.items ?? [];

  return (
    <div className="p-8 max-w-3xl mx-auto">

      {/* Page header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Review Queue</h1>
          <p className="text-gray-500 text-sm mt-1">
            Thread <code className="font-mono text-xs bg-gray-100 px-1.5 py-0.5 rounded">{threadId}</code>
          </p>
        </div>
        <ThreadStatusBadge status="awaiting_review" />
      </div>

      {/* Progress bar */}
      <div className="bg-white rounded-xl border border-gray-200 px-5 py-4 mb-6 flex items-center gap-4">
        <div className="flex-1">
          <div className="flex items-center justify-between text-sm mb-1.5">
            <span className="text-gray-600 font-medium">
              {decidedCount} of {totalCount} decided
            </span>
            {allDecided && (
              <span className="flex items-center gap-1 text-green-700 text-xs font-medium">
                <CheckCircle className="w-3.5 h-3.5" />
                All reviewed
              </span>
            )}
          </div>
          <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
            <div
              className="h-full bg-indigo-500 rounded-full transition-all"
              style={{ width: totalCount > 0 ? `${(decidedCount / totalCount) * 100}%` : "0%" }}
            />
          </div>
        </div>

        <button
          type="button"
          onClick={() => submitDecisions()}
          disabled={!allDecided || isSubmitting}
          className="flex items-center gap-2 px-5 py-2.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition-colors shrink-0"
        >
          {isSubmitting
            ? <><Loader2 className="w-4 h-4 animate-spin" /> Submitting…</>
            : <><Send className="w-4 h-4" /> Submit decisions</>
          }
        </button>
      </div>

      {/* Optimistic lock / submit error */}
      {submitError && (
        <div className="mb-5 flex gap-2 items-start px-4 py-3 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm">
          <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{submitError}</span>
        </div>
      )}

      {/* Review cards */}
      <div className="space-y-4">
        {items.map((item, i) => (
          <ReviewItemCard
            key={item.question_id}
            item={item}
            index={i}
            total={totalCount}
            decision={decisions[item.question_id] ?? null}
            onDecide={handleDecide}
          />
        ))}
      </div>

      {/* Sticky footer submit on long lists */}
      {totalCount > 3 && (
        <div className="sticky bottom-4 mt-6 flex justify-end">
          <button
            type="button"
            onClick={() => submitDecisions()}
            disabled={!allDecided || isSubmitting}
            className="flex items-center gap-2 px-6 py-3 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-medium rounded-xl shadow-lg transition-colors"
          >
            {isSubmitting
              ? <><Loader2 className="w-4 h-4 animate-spin" /> Submitting…</>
              : <><Send className="w-4 h-4" /> Submit {decidedCount} decisions</>
            }
          </button>
        </div>
      )}
    </div>
  );
}
