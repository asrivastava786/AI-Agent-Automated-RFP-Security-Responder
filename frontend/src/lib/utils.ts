import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { WorkflowStatus } from "@/types/api";

/** Merge Tailwind classes safely (resolves conflicts). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Human-readable label for each WorkflowStatus. */
export const STATUS_LABELS: Record<WorkflowStatus, string> = {
  initialised:     "Initialised",
  parsing:         "Parsing",
  retrieving:      "Retrieving",
  drafting:        "Drafting",
  awaiting_review: "Awaiting Review",
  compiling:       "Compiling",
  complete:        "Complete",
  failed:          "Failed",
};

/** Tailwind colour classes for each WorkflowStatus badge. */
export const STATUS_COLOURS: Record<WorkflowStatus, string> = {
  initialised:     "bg-gray-100 text-gray-700",
  parsing:         "bg-blue-100 text-blue-700",
  retrieving:      "bg-blue-100 text-blue-700",
  drafting:        "bg-indigo-100 text-indigo-700",
  awaiting_review: "bg-amber-100 text-amber-800",
  compiling:       "bg-purple-100 text-purple-700",
  complete:        "bg-green-100 text-green-700",
  failed:          "bg-red-100 text-red-700",
};

/** Returns true while the workflow is still running (not terminal). */
export function isProcessing(status: WorkflowStatus): boolean {
  return ["initialised", "parsing", "retrieving", "drafting", "compiling"].includes(status);
}

/** Format a confidence float (0–1) as a percentage string. */
export function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/** Derive the email domain for SAML tenant lookup. */
export function emailDomain(email: string): string {
  return email.split("@")[1] ?? "";
}
