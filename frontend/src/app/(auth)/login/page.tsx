"use client";

/**
 * (auth)/login/page.tsx – Login page with two sign-in paths:
 *
 * 1. Standard Credentials  – email + password + tenantId
 * 2. Enterprise SAML SSO   – work email domain lookup → BoxyHQ Jackson
 *
 * Enterprise SSO flow
 * ───────────────────
 * a. User clicks "Continue with Enterprise SSO" and enters their work email.
 * b. `emailDomain()` extracts "acme.com" from "alice@acme.com".
 * c. `signIn("boxyhq-saml", ..., { tenant: "acme.com", product: "rfp-responder" })`
 *    redirects to BoxyHQ Jackson → Jackson loads the IdP config for "acme.com" →
 *    Jackson redirects to the corporate IdP (Okta, Azure AD, etc.).
 * d. After IdP authentication, control returns to /api/auth/callback/boxyhq-saml,
 *    NextAuth completes the flow and sets the JWT cookie.
 */

import { useState } from "react";
import { signIn } from "next-auth/react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2, ShieldCheck, Building2 } from "lucide-react";
import { emailDomain } from "@/lib/utils";

// ── Form schemas ─────────────────────────────────────────────────────────────

const credentialsSchema = z.object({
  email:    z.string().email("Enter a valid email"),
  password: z.string().min(8, "Min 8 characters"),
  tenantId: z.string().min(1, "Tenant ID is required"),
});

const ssoSchema = z.object({
  workEmail: z.string().email("Enter your work email"),
});

type CredentialsForm = z.infer<typeof credentialsSchema>;
type SSOForm         = z.infer<typeof ssoSchema>;

// ─────────────────────────────────────────────────────────────────────────────

export default function LoginPage() {
  const router       = useRouter();
  const searchParams = useSearchParams();
  const callbackUrl  = searchParams.get("callbackUrl") ?? "/";
  const authError    = searchParams.get("error");

  const [mode, setMode]       = useState<"credentials" | "sso">("credentials");
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState<string | null>(
    authError ? "Authentication failed. Please try again." : null
  );

  // ── Credentials form ───────────────────────────────────────────────────────
  const credForm = useForm<CredentialsForm>({
    resolver: zodResolver(credentialsSchema),
  });

  async function onCredentialsSubmit(values: CredentialsForm) {
    setLoading(true);
    setError(null);
    const result = await signIn("credentials", {
      ...values,
      redirect: false,
    });
    setLoading(false);
    if (result?.error) {
      setError("Invalid credentials. Please check your email and password.");
      return;
    }
    router.push(callbackUrl);
  }

  // ── Enterprise SSO form ────────────────────────────────────────────────────
  const ssoForm = useForm<SSOForm>({
    resolver: zodResolver(ssoSchema),
  });

  async function onSSOSubmit(values: SSOForm) {
    setLoading(true);
    setError(null);
    const domain = emailDomain(values.workEmail);
    if (!domain) {
      setError("Could not determine your email domain.");
      setLoading(false);
      return;
    }
    // Pass tenant (domain) and product to BoxyHQ Jackson via the
    // authorization URL query params. Jackson routes this to the correct IdP.
    await signIn("boxyhq-saml", {
      callbackUrl,
      tenant:  domain,
      product: process.env.NEXT_PUBLIC_SAML_PRODUCT ?? "rfp-responder",
    });
    // signIn with redirect=true (default) – browser navigates away here.
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-slate-900 to-slate-800 px-4">
      <div className="w-full max-w-md">

        {/* Logo / heading */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 bg-indigo-600 rounded-2xl mb-4">
            <ShieldCheck className="w-8 h-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">RFP Responder</h1>
          <p className="text-slate-400 text-sm mt-1">
            Automated Security Questionnaire Platform
          </p>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl shadow-xl p-8">

          {/* Tab switcher */}
          <div className="flex rounded-lg bg-gray-100 p-1 mb-6">
            <button
              onClick={() => { setMode("credentials"); setError(null); }}
              className={`flex-1 py-2 text-sm font-medium rounded-md transition-all ${
                mode === "credentials"
                  ? "bg-white shadow text-gray-900"
                  : "text-gray-500 hover:text-gray-700"
              }`}
            >
              Sign in
            </button>
            <button
              onClick={() => { setMode("sso"); setError(null); }}
              className={`flex-1 py-2 text-sm font-medium rounded-md transition-all flex items-center justify-center gap-1.5 ${
                mode === "sso"
                  ? "bg-white shadow text-gray-900"
                  : "text-gray-500 hover:text-gray-700"
              }`}
            >
              <Building2 className="w-4 h-4" />
              Enterprise SSO
            </button>
          </div>

          {/* Error banner */}
          {error && (
            <div className="mb-4 px-4 py-3 rounded-lg bg-red-50 border border-red-200 text-red-700 text-sm">
              {error}
            </div>
          )}

          {/* ── Credentials form ───────────────────────────────────────────── */}
          {mode === "credentials" && (
            <form onSubmit={credForm.handleSubmit(onCredentialsSubmit)} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Work email
                </label>
                <input
                  type="email"
                  autoComplete="email"
                  placeholder="you@company.com"
                  {...credForm.register("email")}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
                {credForm.formState.errors.email && (
                  <p className="mt-1 text-xs text-red-600">
                    {credForm.formState.errors.email.message}
                  </p>
                )}
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Password
                </label>
                <input
                  type="password"
                  autoComplete="current-password"
                  {...credForm.register("password")}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
                {credForm.formState.errors.password && (
                  <p className="mt-1 text-xs text-red-600">
                    {credForm.formState.errors.password.message}
                  </p>
                )}
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Tenant ID
                </label>
                <input
                  type="text"
                  placeholder="your-org-id"
                  {...credForm.register("tenantId")}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
                {credForm.formState.errors.tenantId && (
                  <p className="mt-1 text-xs text-red-600">
                    {credForm.formState.errors.tenantId.message}
                  </p>
                )}
              </div>

              <button
                type="submit"
                disabled={loading}
                className="w-full flex items-center justify-center gap-2 py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-60 text-white font-medium rounded-lg text-sm transition-colors"
              >
                {loading && <Loader2 className="w-4 h-4 animate-spin" />}
                Sign in
              </button>
            </form>
          )}

          {/* ── Enterprise SSO form ────────────────────────────────────────── */}
          {mode === "sso" && (
            <form onSubmit={ssoForm.handleSubmit(onSSOSubmit)} className="space-y-4">
              <div className="p-4 rounded-lg bg-blue-50 border border-blue-200 text-blue-800 text-sm">
                <p className="font-medium mb-1">Enterprise Single Sign-On</p>
                <p className="text-blue-600">
                  Enter your work email. You'll be redirected to your company's
                  identity provider (Okta, Azure AD, Google Workspace, etc.).
                </p>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Work email
                </label>
                <input
                  type="email"
                  autoComplete="email"
                  placeholder="alice@yourcompany.com"
                  {...ssoForm.register("workEmail")}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
                {ssoForm.formState.errors.workEmail && (
                  <p className="mt-1 text-xs text-red-600">
                    {ssoForm.formState.errors.workEmail.message}
                  </p>
                )}
              </div>

              <button
                type="submit"
                disabled={loading}
                className="w-full flex items-center justify-center gap-2 py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-60 text-white font-medium rounded-lg text-sm transition-colors"
              >
                {loading
                  ? <><Loader2 className="w-4 h-4 animate-spin" /> Redirecting…</>
                  : <><Building2 className="w-4 h-4" /> Continue with SSO</>
                }
              </button>

              <p className="text-center text-xs text-gray-500">
                SSO not configured for your domain?{" "}
                <a href="mailto:support@rfp-responder.io" className="text-indigo-600 hover:underline">
                  Contact your admin
                </a>
              </p>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
