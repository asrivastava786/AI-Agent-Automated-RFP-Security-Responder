/**
 * (dashboard)/page.tsx – Thread list dashboard (Server Component).
 *
 * In a production app this page would fetch the list of threads from a
 * `/api/rfp/threads` endpoint.  For now it renders a shell that the
 * client-side TanStack Query hooks will hydrate.
 */

import { auth } from "@/lib/auth";
import Link from "next/link";
import { Plus, FileText } from "lucide-react";

export default async function DashboardPage() {
  const session = await auth();

  return (
    <div className="p-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">RFP Threads</h1>
          <p className="text-gray-500 text-sm mt-1">
            All questionnaire workflows for <span className="font-medium">{session?.user?.tenantId}</span>
          </p>
        </div>
        <Link
          href="/upload"
          className="flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-lg transition-colors"
        >
          <Plus className="w-4 h-4" />
          New Questionnaire
        </Link>
      </div>

      {/* Empty state (replace with real data fetching) */}
      <div className="bg-white rounded-xl border border-gray-200 p-12 text-center">
        <div className="inline-flex items-center justify-center w-14 h-14 bg-gray-100 rounded-full mb-4">
          <FileText className="w-7 h-7 text-gray-400" />
        </div>
        <h3 className="text-gray-900 font-medium mb-1">No questionnaires yet</h3>
        <p className="text-gray-500 text-sm mb-6">
          Upload a security questionnaire to get started.
        </p>
        <Link
          href="/upload"
          className="inline-flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-lg transition-colors"
        >
          <Plus className="w-4 h-4" />
          Upload questionnaire
        </Link>
      </div>
    </div>
  );
}
