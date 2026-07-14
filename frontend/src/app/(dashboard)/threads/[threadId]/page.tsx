"use client";

/**
 * threads/[threadId]/page.tsx – Live workflow status page.
 *
 * Reached from the upload page immediately after POST /ingest returns.
 * Polls GET /rfp/threads/{threadId}/status every 3 s while the workflow is
 * processing, then auto-navigates to:
 *   → /threads/{threadId}/review   when awaiting_review
 *   → / (dashboard)                when complete
 *   → stays here with error banner when failed
 *
 * The useThreadStatus hook from hooks/use-thread-status.ts handles the
 * polling lifecycle automatically (stops on terminal states).
 */

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import { useSession } from "next-auth/react";
import { useThreadStatus } from "@/hooks/use-thread-status";
import { ThreadStatusBadge } from "@/components/thread-status-badge";
import {
  Loader2, CheckCircle2, AlertTriangle, ArrowRight,
  FileJson, FileSpreadsheet, Clock, Zap, Users,
} from "lucide-react";
import { isProcessing } from "@/lib/utils";
import Link from "next/link";

// ───────────────────────────────────────────────────────���─────────────────────
// Processing step labels (shown as a visual timeline while running)
// ─────────────────────────────────────────────────────────────────────────────

const STEPS = [
  { status: "parsing",         label: "Parsing questionnaire" },
  { status: "retrieving",      label: "Querying Vector DB + Neo4j" },
  { status: "drafting",        label: "Synthesising answers with LLM" },
  { status: "awaiting_review", label: "Routing for human review" },
  { status: "compiling",       label: "Compiling export document" },
  { status: "complete",        label: "Complete" },
] as const;

function stepIndex(status: string): number {
  return STEPS.findIndex((s) => s.status === status);
}

export default function ThreadStatusPage() {
  const { threadId }      = useParams<{ threadId: string }>();
  const { data: session } = useSession();
  const router            = useRouter();
  const tenantId          = session?.user?.tenantId;

  const { data, isLoading, error } = useThreadStatus(tenantId, threadId);

  // Auto-navigate on terminal states
  useEffect(() => {
    if (!data) return;
    if (data.workflow_status === "awaiting_review") {
      router.push(`/threads/${threadId}/review`);
    }
    if (data.workflow_status === "complete") {
      router.push(`/?thread=${threadId}&status=complete`);
    }
  }, [data?.workflow_status, threadId, router]);

  // ── Loading skeleton ──────────────────────────────────────────────────────���
  if (isLoading && !data) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="w-6 h-6 animate-spin text-indigo-600" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8 max-w-xl mx-auto">
        <div className="flex gap-3 items-start px-5 py-4 bg-red-50 border border-red-200 rounded-xl text-red-700">
          <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5" />
          <div>
            <p className="font-medium">Failed to load thread status</p>
            <p className="text-sm mt-0.5">{error.message}</p>
          </div>
        </div>
      </div>
    );
  }

  const status       = data?.workflow_status ?? "initialised";
  const questId      = data?.questionnaire_id ?? "";
  const metrics      = data?.audit_metrics;
  const currentStep  = stepIndex(status);
  const isFailed     = status === "failed";
  const isComplete   = status === "complete";
  const stillRunning = isProcessing(status);

  return (
    <div className="p-8 max-w-xl mx-auto">

      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">
            {isFailed ? "Workflow Failed" : isComplete ? "Complete" : "Processing…"}
          </h1>
          <p className="text-gray-500 text-sm mt-1">
            <span className="font-medium">{questId}</span>
            {" · "}
            <code className="text-xs bg-gray-100 px-1.5 py-0.5 rounded">{threadId}</code>
          </p>
        </div>
        <ThreadStatusBadge status={status} />
      </div>

      {/* Progress timeline */}
      <div className="bg-white rounded-xl border border-gray-200 p-5 mb-5">
        <ol className="space-y-3">
          {STEPS.map((step, i) => {
            const done    = i < currentStep;
            const active  = i === currentStep && stillRunning;
            const pending = i > currentStep;

            return (
              <li key={step.status} className="flex items-center gap-3">
                <div className={`
                  w-6 h-6 rounded-full flex items-center justify-center shrink-0 text-xs font-bold
                  ${done    ? "bg-green-500 text-white"  : ""}
                  ${active  ? "bg-indigo-600 text-white" : ""}
                  ${pending ? "bg-gray-100 text-gray-400": ""}
                `}>
                  {done
                    ? <CheckCircle2 className="w-4 h-4" />
                    : active
                    ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    : <span>{i + 1}</span>
                  }
                </div>
                <span className={`text-sm ${
                  done    ? "text-green-700 font-medium" :
                  active  ? "text-indigo-700 font-semibold" :
                            "text-gray-400"
                }`}>
                  {step.label}
                </span>
              </li>
            );
          })}
        </ol>
      </div>

      {/* Metrics panel (visible once drafting or later) */}
      {metrics && (
        <div className="grid grid-cols-3 gap-3 mb-5">
          <MetricCard
            icon={<Zap className="w-4 h-4 text-green-600" />}
            label="Auto-approved"
            value={`${metrics.auto_approved_count} / ${metrics.total_questions}`}
          />
          <MetricCard
            icon={<Users className="w-4 h-4 text-amber-600" />}
            label="Human reviewed"
            value={String(metrics.human_reviewed_count)}
          />
          <MetricCard
            icon={<Clock className="w-4 h-4 text-blue-600" />}
            label="Duration"
            value={`${metrics.processing_duration_seconds.toFixed(1)}s`}
          />
        </div>
      )}

      {/* Failed state */}
      {isFailed && (
        <div className="flex gap-3 items-start px-5 py-4 bg-red-50 border border-red-200 rounded-xl text-red-700 mb-5">
          <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5" />
          <div>
            <p className="font-medium">Workflow failed</p>
            <p className="text-sm mt-0.5">{data?.error_message ?? "An unexpected error occurred."}</p>
          </div>
        </div>
      )}

      {/* Export links (complete state) */}
      {isComplete && (
        <div className="bg-green-50 border border-green-200 rounded-xl p-5 space-y-3">
          <div className="flex items-center gap-2 text-green-800 font-medium text-sm">
            <CheckCircle2 className="w-4 h-4" />
            All answers exported
          </div>
          <div className="flex gap-3">
            <a
              href={`/api/rfp/export/${questId}.json`}
              className="flex items-center gap-2 px-4 py-2 bg-white border border-green-300 text-green-700 text-sm font-medium rounded-lg hover:bg-green-50 transition-colors"
            >
              <FileJson className="w-4 h-4" /> Download JSON
            </a>
            <a
              href={`/api/rfp/export/${questId}.xlsx`}
              className="flex items-center gap-2 px-4 py-2 bg-white border border-green-300 text-green-700 text-sm font-medium rounded-lg hover:bg-green-50 transition-colors"
            >
              <FileSpreadsheet className="w-4 h-4" /> Download Excel
            </a>
          </div>
        </div>
      )}

      {/* Awaiting review CTA (safety net if auto-nav is slow) */}
      {status === "awaiting_review" && (
        <Link
          href={`/threads/${threadId}/review`}
          className="flex items-center justify-center gap-2 w-full py-3 bg-amber-500 hover:bg-amber-600 text-white font-medium rounded-xl text-sm transition-colors"
        >
          Go to Review Queue
          <ArrowRight className="w-4 h-4" />
        </Link>
      )}
    </div>
  );
}

function MetricCard({
  icon, label, value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 px-4 py-3">
      <div className="flex items-center gap-1.5 mb-1">{icon}
        <span className="text-xs text-gray-500">{label}</span>
      </div>
      <p className="text-lg font-bold text-gray-900">{value}</p>
    </div>
  );
}
