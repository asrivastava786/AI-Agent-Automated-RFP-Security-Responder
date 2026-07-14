"use client";

/**
 * admin/sso/page.tsx – Enterprise SSO Configuration Panel.
 *
 * Allows a tenant administrator to register their company's Identity Provider
 * (IdP) so employees can sign in via SAML SSO.
 *
 * Supported IdPs (via SAML 2.0 / BoxyHQ Jackson):
 *   • Okta
 *   • Microsoft Azure Active Directory / Entra ID
 *   • Google Workspace
 *   • OneLogin
 *   • Any SAML 2.0-compliant IdP
 *
 * Two configuration methods
 * ──────────────────────────
 * 1. Metadata XML paste  – paste the raw XML from the IdP admin console.
 * 2. Metadata URL        – provide the URL; BoxyHQ Jackson fetches and
 *                          caches the XML automatically.
 *
 * What happens on submit
 * ──────────────────────
 * The form POSTs to /api/sso/configure (a Next.js API route) which proxies
 * the request to the BoxyHQ Jackson admin API using the server-side
 * BOXYHQ_SAML_ADMIN_SECRET.  The secret never leaves the server.
 *
 * After setup, employees can sign in by:
 *   1. Visiting /login → clicking "Enterprise SSO"
 *   2. Entering their work email (e.g. alice@acme.com)
 *   3. Being redirected to the company's IdP automatically
 */

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useSession } from "next-auth/react";
import { ssoApi } from "@/lib/api";
import {
  Building2, CheckCircle2, AlertCircle, Loader2,
  ExternalLink, Info, KeyRound, Copy, Check
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { SSOConfigResponse } from "@/types/api";

// ── Form schema ───────────────────────────────────────────────────────────────

const ssoConfigSchema = z.object({
  name: z.string().min(1, "Display name is required"),
  tenant: z
    .string()
    .min(1, "Domain is required")
    .regex(/^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/, "Enter a valid domain (e.g. acme.com)"),
  method: z.enum(["xml", "url"]),
  metadataXml: z.string().optional(),
  metadataUrl: z.string().url("Enter a valid URL").optional(),
}).superRefine((data, ctx) => {
  if (data.method === "xml" && !data.metadataXml?.trim()) {
    ctx.addIssue({ code: "custom", path: ["metadataXml"], message: "Metadata XML is required" });
  }
  if (data.method === "url" && !data.metadataUrl?.trim()) {
    ctx.addIssue({ code: "custom", path: ["metadataUrl"], message: "Metadata URL is required" });
  }
});

type SSOConfigForm = z.infer<typeof ssoConfigSchema>;

// ─────────────────────────────────────────────────────────────────────────────

const IDP_GUIDES: Array<{ name: string; docsUrl: string; hint: string }> = [
  {
    name: "Okta",
    docsUrl: "https://developer.okta.com/docs/guides/build-sso-integration/saml2/main/",
    hint: "In Okta: Applications → Create App Integration → SAML 2.0. Download the IdP metadata XML from the Sign On tab.",
  },
  {
    name: "Azure AD / Entra ID",
    docsUrl: "https://learn.microsoft.com/en-us/entra/identity/saas-apps/tutorial-list",
    hint: "In Azure Portal: Enterprise Applications → New Application → Non-gallery. Set up SSO → SAML. Download Federation Metadata XML.",
  },
  {
    name: "Google Workspace",
    docsUrl: "https://support.google.com/a/answer/6087519",
    hint: "In Google Admin: Apps → Web and mobile apps → Add SAML app. Copy the SSO URL and Certificate from the Google IdP details page.",
  },
];

export default function SSOAdminPage() {
  const { data: session } = useSession();
  const tenantDomain = session?.user?.tenantId ?? "";

  const [savedConfig, setSavedConfig] = useState<SSOConfigResponse | null>(null);
  const [loading, setLoading]         = useState(false);
  const [error, setError]             = useState<string | null>(null);
  const [copiedField, setCopiedField] = useState<string | null>(null);

  const { register, handleSubmit, watch, formState } = useForm<SSOConfigForm>({
    resolver: zodResolver(ssoConfigSchema),
    defaultValues: {
      name:   "",
      tenant: tenantDomain,
      method: "url",
    },
  });

  const method = watch("method");

  async function onSubmit(values: SSOConfigForm) {
    setLoading(true);
    setError(null);
    try {
      const config = await ssoApi.configure({
        name:             values.name,
        tenant:           values.tenant,
        product:          process.env.NEXT_PUBLIC_SAML_PRODUCT ?? "rfp-responder",
        rawMetadata:      values.method === "xml" ? values.metadataXml : undefined,
        metadataUrl:      values.method === "url" ? values.metadataUrl : undefined,
        defaultRedirectUrl: `${window.location.origin}/`,
        redirectUrl:        `${window.location.origin}/api/auth/callback/boxyhq-saml`,
      });
      setSavedConfig(config);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Configuration failed. Check your metadata.");
    } finally {
      setLoading(false);
    }
  }

  async function copyToClipboard(text: string, field: string) {
    await navigator.clipboard.writeText(text);
    setCopiedField(field);
    setTimeout(() => setCopiedField(null), 2000);
  }

  return (
    <div className="p-8 max-w-2xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-900">Enterprise SSO</h1>
        <p className="text-gray-500 text-sm mt-1">
          Connect your company's Identity Provider so employees can sign in with their
          corporate credentials via SAML 2.0.
        </p>
      </div>

      {/* ── Success state ────────────────────────────────────────────────── */}
      {savedConfig ? (
        <SuccessPanel
          config={savedConfig}
          copiedField={copiedField}
          onCopy={copyToClipboard}
          onReset={() => setSavedConfig(null)}
        />
      ) : (
        <div className="space-y-6">

          {/* IdP guides */}
          <div className="bg-blue-50 rounded-xl border border-blue-200 p-4">
            <div className="flex items-center gap-2 text-blue-800 font-medium text-sm mb-3">
              <Info className="w-4 h-4" />
              Setup guides for common Identity Providers
            </div>
            <div className="space-y-2">
              {IDP_GUIDES.map((g) => (
                <details key={g.name} className="group">
                  <summary className="flex items-center justify-between cursor-pointer text-sm text-blue-700 font-medium hover:text-blue-900 list-none">
                    {g.name}
                    <ExternalLink className="w-3.5 h-3.5 opacity-60" />
                  </summary>
                  <p className="mt-1.5 text-xs text-blue-600 pl-2 border-l-2 border-blue-300">
                    {g.hint}{" "}
                    <a href={g.docsUrl} target="_blank" rel="noreferrer" className="underline hover:text-blue-800">
                      Full docs →
                    </a>
                  </p>
                </details>
              ))}
            </div>
          </div>

          {/* Configuration form */}
          <div className="bg-white rounded-xl border border-gray-200 p-6">
            <form onSubmit={handleSubmit(onSubmit)} className="space-y-5">

              {/* Display name */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Configuration name
                </label>
                <input
                  type="text"
                  placeholder="Acme Corp Okta"
                  {...register("name")}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
                {formState.errors.name && (
                  <p className="mt-1 text-xs text-red-600">{formState.errors.name.message}</p>
                )}
              </div>

              {/* Tenant domain */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Company domain
                  <span className="ml-1 text-xs text-gray-400 font-normal">
                    (used to route SSO logins)
                  </span>
                </label>
                <div className="relative">
                  <Building2 className="absolute left-3 top-2.5 w-4 h-4 text-gray-400" />
                  <input
                    type="text"
                    placeholder="acme.com"
                    {...register("tenant")}
                    className="w-full pl-9 pr-3 py-2 rounded-lg border border-gray-300 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                </div>
                {formState.errors.tenant && (
                  <p className="mt-1 text-xs text-red-600">{formState.errors.tenant.message}</p>
                )}
                <p className="mt-1 text-xs text-gray-400">
                  Employees with @acme.com emails will be routed to this IdP.
                </p>
              </div>

              {/* Method selector */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  IdP metadata source
                </label>
                <div className="flex gap-2">
                  {(["url", "xml"] as const).map((m) => (
                    <label
                      key={m}
                      className={cn(
                        "flex-1 flex items-center justify-center gap-2 py-2 px-3 rounded-lg border text-sm font-medium cursor-pointer transition-all",
                        method === m
                          ? "bg-indigo-50 border-indigo-500 text-indigo-700"
                          : "bg-white border-gray-200 text-gray-600 hover:border-gray-300"
                      )}
                    >
                      <input type="radio" value={m} {...register("method")} className="sr-only" />
                      {m === "url" ? "Metadata URL" : "Paste XML"}
                    </label>
                  ))}
                </div>
              </div>

              {/* Metadata URL */}
              {method === "url" && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    IdP metadata URL
                  </label>
                  <input
                    type="url"
                    placeholder="https://your-idp.example.com/metadata"
                    {...register("metadataUrl")}
                    className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                  {formState.errors.metadataUrl && (
                    <p className="mt-1 text-xs text-red-600">{formState.errors.metadataUrl.message}</p>
                  )}
                </div>
              )}

              {/* Metadata XML */}
              {method === "xml" && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    IdP metadata XML
                  </label>
                  <textarea
                    rows={8}
                    placeholder={'<?xml version="1.0"?>\n<EntityDescriptor ...>'}
                    {...register("metadataXml")}
                    className="w-full px-3 py-2 rounded-lg border border-gray-300 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none"
                  />
                  {formState.errors.metadataXml && (
                    <p className="mt-1 text-xs text-red-600">{formState.errors.metadataXml.message}</p>
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

              <button
                type="submit"
                disabled={loading}
                className="w-full flex items-center justify-center gap-2 py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-medium rounded-lg text-sm transition-colors"
              >
                {loading
                  ? <><Loader2 className="w-4 h-4 animate-spin" /> Saving…</>
                  : <><KeyRound className="w-4 h-4" /> Save SSO Configuration</>
                }
              </button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Post-save success panel ────────────────────────────────────────────────────

function SuccessPanel({
  config,
  copiedField,
  onCopy,
  onReset,
}: {
  config: SSOConfigResponse;
  copiedField: string | null;
  onCopy: (text: string, field: string) => void;
  onReset: () => void;
}) {
  const acsUrl = `${typeof window !== "undefined" ? window.location.origin : ""}/api/auth/callback/boxyhq-saml`;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 px-5 py-4 bg-green-50 border border-green-200 rounded-xl">
        <CheckCircle2 className="w-5 h-5 text-green-600 shrink-0" />
        <div>
          <p className="text-green-800 font-medium text-sm">SSO configured successfully</p>
          <p className="text-green-600 text-xs mt-0.5">
            Employees with @{config.tenant} emails can now sign in via your IdP.
          </p>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 p-5 space-y-4">
        <p className="text-sm font-medium text-gray-700">
          Service Provider details to register in your IdP:
        </p>

        <CopyRow
          label="ACS / Reply URL"
          value={acsUrl}
          field="acs"
          copiedField={copiedField}
          onCopy={onCopy}
        />
        <CopyRow
          label="Entity ID / Audience URI"
          value={config.idpMetadata?.entityID ?? ""}
          field="entity"
          copiedField={copiedField}
          onCopy={onCopy}
        />
        {config.idpMetadata?.sso?.postUrl && (
          <CopyRow
            label="IdP SSO URL"
            value={config.idpMetadata.sso.postUrl}
            field="sso"
            copiedField={copiedField}
            onCopy={onCopy}
          />
        )}
      </div>

      <button
        onClick={onReset}
        className="text-sm text-indigo-600 hover:text-indigo-800 font-medium"
      >
        ← Configure another IdP
      </button>
    </div>
  );
}

function CopyRow({
  label,
  value,
  field,
  copiedField,
  onCopy,
}: {
  label: string;
  value: string;
  field: string;
  copiedField: string | null;
  onCopy: (text: string, field: string) => void;
}) {
  return (
    <div>
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <div className="flex items-center gap-2 px-3 py-2 bg-gray-50 rounded-lg border border-gray-200">
        <code className="flex-1 text-xs text-gray-800 font-mono truncate">{value}</code>
        <button
          type="button"
          onClick={() => onCopy(value, field)}
          className="shrink-0 text-gray-400 hover:text-gray-700 transition-colors"
        >
          {copiedField === field
            ? <Check className="w-4 h-4 text-green-600" />
            : <Copy className="w-4 h-4" />
          }
        </button>
      </div>
    </div>
  );
}
