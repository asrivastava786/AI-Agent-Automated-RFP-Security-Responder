"use client";

import { useState } from "react";
import { ChevronDown, ChevronUp, CheckCircle2, XCircle, Pencil, AlertTriangle, ShieldCheck, ShieldX } from "lucide-react";
import { ConfidenceBar } from "./confidence-bar";
import { cn } from "@/lib/utils";
import type { DecisionType, ReviewItem } from "@/types/api";

export interface CardDecision {
  type: DecisionType;
  overrideText?: string;
  notes?: string;
}

interface ReviewItemCardProps {
  item: ReviewItem;
  index: number;
  total: number;
  decision: CardDecision | null;
  onDecide: (questionId: string, decision: CardDecision) => void;
}

/**
 * Card for a single flagged review item.
 *
 * Three decision buttons:
 *   ✓ Approve      – accept the proposed answer as-is
 *   ✎ Override     – expand inline textarea; submit custom answer
 *   ✗ Reject       – exclude this question from the export
 *
 * Decision state is lifted up to the parent page so the "Submit all" button
 * can aggregate decisions across all cards in one POST /resume call.
 */
export function ReviewItemCard({
  item,
  index,
  total,
  decision,
  onDecide,
}: ReviewItemCardProps) {
  const [expanded, setExpanded]         = useState(false);   // reasoning trace
  const [overrideOpen, setOverrideOpen] = useState(false);   // override textarea
  const [overrideText, setOverrideText] = useState(item.proposed_answer);
  const [notes, setNotes]               = useState("");

  function handleApprove() {
    setOverrideOpen(false);
    onDecide(item.question_id, { type: "approve" });
  }

  function handleOverride() {
    if (!overrideOpen) { setOverrideOpen(true); return; }
    if (!overrideText.trim()) return;
    onDecide(item.question_id, { type: "override", overrideText, notes });
    setOverrideOpen(false);
  }

  function handleReject() {
    setOverrideOpen(false);
    onDecide(item.question_id, { type: "reject", notes });
  }

  const decisionColour =
    decision?.type === "approve"  ? "border-l-4 border-green-500" :
    decision?.type === "override" ? "border-l-4 border-amber-500" :
    decision?.type === "reject"   ? "border-l-4 border-red-500"   :
    "border-l-4 border-gray-200";

  return (
    <div className={cn("bg-white rounded-xl border border-gray-200 overflow-hidden", decisionColour)}>
      {/* Header */}
      <div className="px-5 py-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1.5 flex-wrap">
              <span className="text-xs text-gray-400 font-medium">
                {index + 1} / {total}
              </span>
              {item.control_id && (
                <span className="px-2 py-0.5 bg-indigo-50 text-indigo-700 text-xs font-medium rounded">
                  {item.control_id}
                </span>
              )}
              <span className="px-2 py-0.5 bg-gray-100 text-gray-600 text-xs rounded">
                {item.category}
              </span>
              {item.discrepancy_detected && (
                <span className="flex items-center gap-1 px-2 py-0.5 bg-amber-50 text-amber-700 text-xs font-medium rounded">
                  <AlertTriangle className="w-3 h-3" />
                  Discrepancy
                </span>
              )}
            </div>
            <p className="text-gray-900 font-medium text-sm">{item.question_text}</p>
          </div>

          {decision && (
            <DecisionChip type={decision.type} />
          )}
        </div>
      </div>

      {/* Proposed answer */}
      <div className="px-5 pb-4 border-t border-gray-100">
        <p className="text-xs text-gray-500 font-medium mt-3 mb-1.5">Proposed answer</p>
        <p className="text-sm text-gray-800 leading-relaxed bg-gray-50 rounded-lg px-3 py-2.5">
          {item.proposed_answer}
        </p>
      </div>

      {/* Signals */}
      <div className="px-5 pb-4 grid grid-cols-2 gap-x-6 gap-y-2">
        <div>
          <p className="text-xs text-gray-500 mb-1">Vector confidence</p>
          <ConfidenceBar value={item.vector_confidence} />
        </div>
        <div>
          <p className="text-xs text-gray-500 mb-1">Graph verified</p>
          <div className="flex items-center gap-1.5">
            {item.graph_verified
              ? <ShieldCheck className="w-4 h-4 text-green-600" />
              : <ShieldX     className="w-4 h-4 text-red-500"   />
            }
            <span className={cn("text-xs font-medium", item.graph_verified ? "text-green-700" : "text-red-600")}>
              {item.graph_verified ? "Verified" : "Not verified"}
            </span>
          </div>
        </div>
      </div>

      {/* Reasoning trace (collapsible) */}
      <div className="px-5 pb-4">
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800 font-medium"
        >
          {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          {expanded ? "Hide reasoning" : "View reasoning"}
        </button>
        {expanded && (
          <p className="mt-2 text-xs text-gray-600 leading-relaxed bg-blue-50 rounded-lg px-3 py-2.5 border border-blue-100">
            {item.reasoning_trace}
          </p>
        )}
      </div>

      {/* Override textarea */}
      {overrideOpen && (
        <div className="px-5 pb-4 border-t border-amber-100 bg-amber-50">
          <p className="text-xs font-medium text-amber-800 mt-3 mb-1.5">Override answer</p>
          <textarea
            rows={4}
            value={overrideText}
            onChange={(e) => setOverrideText(e.target.value)}
            className="w-full px-3 py-2 text-sm rounded-lg border border-amber-300 bg-white focus:outline-none focus:ring-2 focus:ring-amber-500 resize-none"
          />
          <input
            type="text"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Optional: note why you changed this answer"
            className="mt-2 w-full px-3 py-2 text-xs rounded-lg border border-amber-200 bg-white focus:outline-none focus:ring-1 focus:ring-amber-400"
          />
        </div>
      )}

      {/* Action buttons */}
      <div className="px-5 py-3 bg-gray-50 border-t border-gray-100 flex gap-2">
        <button
          type="button"
          onClick={handleApprove}
          className={cn(
            "flex-1 flex items-center justify-center gap-1.5 py-2 text-xs font-medium rounded-lg transition-all",
            decision?.type === "approve"
              ? "bg-green-600 text-white"
              : "bg-white border border-gray-300 text-gray-700 hover:bg-green-50 hover:border-green-400 hover:text-green-700"
          )}
        >
          <CheckCircle2 className="w-3.5 h-3.5" />
          Approve
        </button>

        <button
          type="button"
          onClick={handleOverride}
          className={cn(
            "flex-1 flex items-center justify-center gap-1.5 py-2 text-xs font-medium rounded-lg transition-all",
            decision?.type === "override"
              ? "bg-amber-500 text-white"
              : overrideOpen
              ? "bg-amber-500 text-white"
              : "bg-white border border-gray-300 text-gray-700 hover:bg-amber-50 hover:border-amber-400 hover:text-amber-700"
          )}
        >
          <Pencil className="w-3.5 h-3.5" />
          {overrideOpen ? "Confirm override" : "Override"}
        </button>

        <button
          type="button"
          onClick={handleReject}
          className={cn(
            "flex-1 flex items-center justify-center gap-1.5 py-2 text-xs font-medium rounded-lg transition-all",
            decision?.type === "reject"
              ? "bg-red-600 text-white"
              : "bg-white border border-gray-300 text-gray-700 hover:bg-red-50 hover:border-red-400 hover:text-red-700"
          )}
        >
          <XCircle className="w-3.5 h-3.5" />
          Reject
        </button>
      </div>
    </div>
  );
}

function DecisionChip({ type }: { type: DecisionType }) {
  const styles = {
    approve:  "bg-green-100 text-green-700",
    override: "bg-amber-100 text-amber-700",
    reject:   "bg-red-100   text-red-700",
  };
  const labels = { approve: "Approved", override: "Overridden", reject: "Rejected" };
  return (
    <span className={cn("shrink-0 px-2 py-0.5 rounded-full text-xs font-semibold", styles[type])}>
      {labels[type]}
    </span>
  );
}
