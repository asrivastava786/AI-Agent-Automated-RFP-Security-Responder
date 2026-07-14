"use client";

/**
 * upload/page.tsx – Questionnaire ingestion form.
 *
 * Two input modes (tabs):
 *   JSON paste – paste a JSON array of question objects.
 *   Excel upload – drag-and-drop an .xlsx file (converted to base64).
 *
 * On submit, calls POST /api/rfp/ingest and navigates to the review page
 * (HTTP 202) or the export page (HTTP 200).
 */

import { useState, useCallback } from "react";
import { useDropzone } from "react-dropzone";
import { useSession } from "next-auth/react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useRouter } from "next/navigation";
import { Upload, FileSpreadsheet, Code2, Loader2, AlertCircle } from "lucide-react";
import { rfpApi } from "@/lib/api";
import type { IngestPayload } from "@/types/api";

// ── Form schema ───────────────────────────────────────────────────────────────

const uploadSchema = z.object({
  questionnaireId: z
    .string()
    .min(1, "Questionnaire ID is required")
    .regex(/^[a-zA-Z0-9_-]+$/, "Only letters, numbers, hyphens, underscores"),
});

type UploadForm = z.infer<typeof uploadSchema>;

// ─────────────────────────────────────────────────────────────────────────────

export default function UploadPage() {
  const { data: session } = useSession();
  const router = useRouter();

  const [mode, setMode]       = useState<"json" | "excel">("json");
  const [jsonText, setJsonText] = useState("");
  const [excelFile, setExcelFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState<string | null>(null);

  const { register, handleSubmit, formState } = useForm<UploadForm>({
    resolver: zodResolver(uploadSchema),
  });

  // ── Dropzone ────────────────────────────────────────────────────────────────
  const onDrop = useCallback((accepted: File[]) => {
    if (accepted[0]) setExcelFile(accepted[0]);
  }, []);
  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"] },
    maxFiles: 1,
  });

  // ── Submit ──────────────────────────────────────────────────────────────────
  async function onSubmit(values: UploadForm) {
    if (!session?.user?.tenantId) return;
    setLoading(true);
    setError(null);

    let payload: IngestPayload;

    try {
      if (mode === "json") {
        const parsed = JSON.parse(jsonText);
        const questions = Array.isArray(parsed) ? parsed : parsed.questions;
        if (!Array.isArray(questions)) throw new Error("Expected a JSON array of questions.");
        payload = { format: "json", questions };
      } else {
        if (!excelFile) throw new Error("No file selected.");
        const arrayBuffer = await excelFile.arrayBuffer();
        const base64 = btoa(
          new Uint8Array(arrayBuffer).reduce((s, b) => s + String.fromCharCode(b), "")
        );
        payload = { format: "excel", file_content: base64 };
      }

      const result = await rfpApi.ingest(session.user.tenantId, {
        questionnaire_id: values.questionnaireId,
        payload,
      });

      if (result.status === "awaiting_review") {
        router.push(`/threads/${result.thread_id}/review`);
      } else {
        router.push(`/?thread=${result.thread_id}&status=complete`);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Upload failed. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="p-8 max-w-2xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-900">New Questionnaire</h1>
        <p className="text-gray-500 text-sm mt-1">
          Upload a security questionnaire to start the automated response workflow.
        </p>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 p-6 space-y-6">
        {/* Questionnaire ID */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Questionnaire ID
          </label>
          <input
            type="text"
            placeholder="rfp-acme-2024-q4"
            {...register("questionnaireId")}
            className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          />
          {formState.errors.questionnaireId && (
            <p className="mt-1 text-xs text-red-600">
              {formState.errors.questionnaireId.message}
            </p>
          )}
        </div>

        {/* Input mode tabs */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Input format
          </label>
          <div className="flex gap-2">
            {(["json", "excel"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium border transition-all ${
                  mode === m
                    ? "bg-indigo-50 border-indigo-500 text-indigo-700"
                    : "bg-white border-gray-200 text-gray-600 hover:border-gray-300"
                }`}
              >
                {m === "json"
                  ? <><Code2 className="w-4 h-4" /> JSON</>
                  : <><FileSpreadsheet className="w-4 h-4" /> Excel (.xlsx)</>
                }
              </button>
            ))}
          </div>
        </div>

        {/* JSON paste area */}
        {mode === "json" && (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Questions JSON
            </label>
            <textarea
              rows={10}
              value={jsonText}
              onChange={(e) => setJsonText(e.target.value)}
              placeholder={`[\n  { "question_text": "Do you support SAML SSO?", "category": "Authentication" },\n  { "question_text": "Is data encrypted at rest?", "category": "Encryption", "control_id": "SOC2-CC6.7" }\n]`}
              className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none"
            />
          </div>
        )}

        {/* Excel dropzone */}
        {mode === "excel" && (
          <div
            {...getRootProps()}
            className={`border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition-colors ${
              isDragActive
                ? "border-indigo-500 bg-indigo-50"
                : "border-gray-300 hover:border-indigo-400 bg-gray-50"
            }`}
          >
            <input {...getInputProps()} />
            <Upload className="w-8 h-8 text-gray-400 mx-auto mb-3" />
            {excelFile ? (
              <p className="text-sm font-medium text-gray-900">{excelFile.name}</p>
            ) : (
              <>
                <p className="text-sm font-medium text-gray-700">
                  Drop your .xlsx file here, or click to browse
                </p>
                <p className="text-xs text-gray-500 mt-1">
                  First row must be a header row.
                </p>
              </>
            )}
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="flex gap-2 items-start px-4 py-3 rounded-lg bg-red-50 border border-red-200 text-red-700 text-sm">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            {error}
          </div>
        )}

        {/* Submit */}
        <button
          type="button"
          onClick={handleSubmit(onSubmit)}
          disabled={loading || (mode === "json" && !jsonText.trim()) || (mode === "excel" && !excelFile)}
          className="w-full flex items-center justify-center gap-2 py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-medium rounded-lg text-sm transition-colors"
        >
          {loading
            ? <><Loader2 className="w-4 h-4 animate-spin" /> Processing…</>
            : "Start Workflow"
          }
        </button>
      </div>
    </div>
  );
}
