/**
 * lib/api.ts – Fully-typed Axios client for the FastAPI backend.
 *
 * All requests are routed through the Next.js rewrite rule in next.config.ts:
 *   /api/rfp/*  →  {BACKEND_URL}/api/v1/rfp/*
 *
 * This means:
 *   a) The browser never needs to know the backend origin.
 *   b) The X-Tenant-ID and Authorization headers are added once here,
 *      automatically on every request.
 *   c) CORS is a non-issue – the browser talks to the same origin as the UI.
 *
 * Usage
 * ─────
 *   import { rfpApi } from "@/lib/api"
 *   const response = await rfpApi.ingest(tenantId, body)
 */

import axios, { type AxiosInstance, type AxiosResponse } from "axios";
import type {
  IngestRequest,
  IngestResponse,
  ResumeRequest,
  ResumeResponse,
  ReviewItemsResponse,
  SSOConfigRequest,
  SSOConfigResponse,
  ThreadStatusResponse,
} from "@/types/api";

// ─────────────────────────────────────────────────────────────────────────────
// Axios instance
// ─────────────────────────────────────────────────────────────────────────────

const http: AxiosInstance = axios.create({
  baseURL: "/api/rfp",
  headers: { "Content-Type": "application/json" },
  // 60s timeout – synthesis can be slow for large questionnaires
  timeout: 60_000,
});

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

/** Build request config with tenant header injected. */
function withTenant(tenantId: string) {
  return { headers: { "X-Tenant-ID": tenantId } };
}

function data<T>(res: AxiosResponse<T>): T {
  return res.data;
}

// ─────────────────────────────────────────────────────────────────────────────
// RFP Workflow API
// ─────────────────────────────────────────────────────────────────────────────

export const rfpApi = {
  /**
   * POST /rfp/ingest
   * Start a new RFP workflow thread.
   * Returns 200 (complete) or 202 (awaiting review).
   * Axios treats both as resolved (no throw); callers check `status` field.
   */
  async ingest(tenantId: string, body: IngestRequest): Promise<IngestResponse> {
    const res = await http.post<IngestResponse>(
      "/ingest",
      body,
      {
        ...withTenant(tenantId),
        // Accept 202 as a success – Axios throws by default for non-2xx
        validateStatus: (s) => s === 200 || s === 202,
      }
    );
    return data(res);
  },

  /** GET /rfp/threads/{threadId}/status */
  async getStatus(tenantId: string, threadId: string): Promise<ThreadStatusResponse> {
    const res = await http.get<ThreadStatusResponse>(
      `/threads/${threadId}/status`,
      withTenant(tenantId)
    );
    return data(res);
  },

  /** GET /rfp/threads/{threadId}/review */
  async getReviewItems(tenantId: string, threadId: string): Promise<ReviewItemsResponse> {
    const res = await http.get<ReviewItemsResponse>(
      `/threads/${threadId}/review`,
      withTenant(tenantId)
    );
    return data(res);
  },

  /**
   * POST /rfp/threads/{threadId}/resume
   * Submit human decisions and resume the graph.
   * Returns 200 (complete) or 202 (another review wave).
   */
  async resume(
    tenantId: string,
    threadId: string,
    body: ResumeRequest
  ): Promise<ResumeResponse> {
    const res = await http.post<ResumeResponse>(
      `/threads/${threadId}/resume`,
      body,
      {
        ...withTenant(tenantId),
        validateStatus: (s) => s === 200 || s === 202,
      }
    );
    return data(res);
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// SSO Admin API  (calls BoxyHQ Jackson directly via the Next.js API route proxy)
// The Next.js API route at /api/sso/* proxies to the Jackson admin endpoint
// so the admin secret never leaks to the browser.
// ─────────────────────────────────────────────────────────────────────────────

const ssoHttp: AxiosInstance = axios.create({
  baseURL: "/api/sso",
  headers: { "Content-Type": "application/json" },
  timeout: 15_000,
});

export const ssoApi = {
  /**
   * Register (or update) a SAML SSO configuration for an enterprise tenant.
   * Proxied through Next.js to avoid exposing the Jackson admin secret.
   */
  async configure(body: SSOConfigRequest): Promise<SSOConfigResponse> {
    const res = await ssoHttp.post<SSOConfigResponse>("/configure", body);
    return data(res);
  },

  /** Delete an existing SSO configuration. */
  async remove(tenant: string, product: string): Promise<void> {
    await ssoHttp.delete("/configure", { params: { tenant, product } });
  },

  /** List all SSO configs for the current super-admin. */
  async list(): Promise<SSOConfigResponse[]> {
    const res = await ssoHttp.get<SSOConfigResponse[]>("/configure");
    return data(res);
  },
};
