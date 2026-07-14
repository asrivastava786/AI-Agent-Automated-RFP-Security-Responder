/**
 * lib/auth.ts – NextAuth v5 (Auth.js) configuration
 *
 * Two authentication providers are wired:
 *
 * 1. Credentials  – email + password + tenantId for standard (non-enterprise) users.
 *    In production replace `mockVerifyCredentials` with a real DB lookup and
 *    bcrypt comparison.
 *
 * 2. BoxyHQ SAML  – SAML 2.0 Single Sign-On for enterprise tenants.
 *
 *    Architecture
 *    ────────────
 *    BoxyHQ SAML Jackson (https://boxyhq.com/docs/jackson/overview) acts as a
 *    SAML → OAuth 2.0 proxy. This means NextAuth never handles raw SAML
 *    assertions; it treats Jackson as a standard OAuth 2.0 / OIDC provider.
 *
 *    Flow for an enterprise user
 *    ───────────────────────────
 *    a. User visits /login, enters work email (e.g. alice@acme.com).
 *    b. Frontend extracts the domain ("acme.com"), builds the Jackson
 *       authorize URL with ?tenant=acme.com&product=rfp-responder.
 *    c. NextAuth redirects to Jackson → Jackson redirects to the
 *       corporate IdP (Okta / Azure AD / Google Workspace).
 *    d. IdP authenticates the user, returns a SAML assertion to Jackson.
 *    e. Jackson converts the assertion to an OAuth 2.0 code, returns to NextAuth.
 *    f. NextAuth exchanges the code, gets an ID token, extracts claims.
 *    g. The JWT callback stamps tenant_id from the Jackson profile claim.
 *    h. Every subsequent API call carries X-Tenant-ID from session.user.tenantId.
 *
 *    Per-tenant IdP setup
 *    ────────────────────
 *    Enterprise admins paste their IdP metadata XML (or URL) into the admin
 *    panel (/admin/sso). The frontend POSTs it to the Jackson admin API:
 *      POST {BOXYHQ_SAML_ISSUER}/api/v1/saml/config
 *    Jackson stores the per-tenant config and handles routing automatically.
 */

import NextAuth, { type DefaultSession } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";
import BoxyHQSAMLProvider from "next-auth/providers/boxyhq-saml";
import { z } from "zod";

// ─────────────────────────────────────────────────────────────────────────────
// Type augmentation – adds tenantId and provider to the session
// (also declared in types/next-auth.d.ts for module-level visibility)
// ─────────────────────────────────────────────────────────────────────────────

declare module "next-auth" {
  interface Session {
    user: DefaultSession["user"] & {
      tenantId: string;
      provider: "credentials" | "saml";
    };
  }
  interface User {
    tenantId: string;
    provider: "credentials" | "saml";
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Credentials schema
// ─────────────────────────────────────────────────────────────────────────────

const credentialsSchema = z.object({
  email: z.string().email("Invalid email address"),
  password: z.string().min(8, "Password must be at least 8 characters"),
  tenantId: z.string().min(1, "Tenant ID is required"),
});

/**
 * Mock credential verifier – replace with real DB + bcrypt in production.
 * Returns null on failure (NextAuth treats null as auth failed).
 */
async function mockVerifyCredentials(
  email: string,
  _password: string,
  tenantId: string
): Promise<{ id: string; email: string; name: string; tenantId: string } | null> {
  // Production: query your user store, verify bcrypt hash, check tenant membership
  if (!email || !tenantId) return null;
  return {
    id: `usr_${tenantId}_${email.split("@")[0]}`,
    email,
    name: email.split("@")[0],
    tenantId,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// NextAuth configuration
// ─────────────────────────────────────────────────────────────────────────────

export const { handlers, auth, signIn, signOut } = NextAuth({
  secret: process.env.AUTH_SECRET,
  // Deployed behind hosts (e.g. shared hosting, reverse proxies) that don't
  // match NEXTAUTH_URL/AUTH_URL exactly. Without this, Auth.js rejects every
  // request with "UntrustedHost" and auth() silently returns undefined.
  trustHost: true,

  providers: [
    // ── 1. Standard credentials ─────────────────────────────────────────────
    CredentialsProvider({
      name: "Credentials",
      credentials: {
        email:    { label: "Work email",  type: "email"    },
        password: { label: "Password",    type: "password" },
        tenantId: { label: "Tenant ID",   type: "text"     },
      },
      async authorize(credentials) {
        const parsed = credentialsSchema.safeParse(credentials);
        if (!parsed.success) return null;

        const { email, password, tenantId } = parsed.data;
        const user = await mockVerifyCredentials(email, password, tenantId);
        if (!user) return null;

        return { ...user, provider: "credentials" as const };
      },
    }),

    // ── 2. Enterprise SAML SSO via BoxyHQ Jackson ───────────────────────────
    //
    // BoxyHQSAMLProvider ships with next-auth as a first-class provider.
    // `issuer` points at the Jackson instance (self-hosted or BoxyHQ cloud).
    // `clientId` / `clientSecret` are the OAuth 2.0 credentials Jackson issued.
    //
    // The `tenant` and `product` query params in authorization.params tell
    // Jackson which IdP configuration to load for this login attempt.
    // The tenant is set dynamically in the login page before redirecting.
    BoxyHQSAMLProvider({
      issuer:       process.env.BOXYHQ_SAML_ISSUER!,
      clientId:     process.env.BOXYHQ_SAML_CLIENT_ID!,
      clientSecret: process.env.BOXYHQ_SAML_CLIENT_SECRET!,
      authorization: {
        params: {
          // `product` is constant across all enterprise tenants.
          // `tenant` is set per-login in the frontend via a dynamic signIn() call.
          product: process.env.NEXT_PUBLIC_SAML_PRODUCT ?? "rfp-responder",
          scope:   "openid email profile",
        },
      },
      // Jackson returns tenant info in the profile's `requested` object.
      profile(profile) {
        return {
          id:       profile.id ?? profile.sub,
          name:     profile.name,
          email:    profile.email,
          image:    profile.image ?? null,
          tenantId: (profile as Record<string, unknown>).requested
            ? (profile as Record<string, { tenant: string }>).requested.tenant
            : "",
          provider: "saml" as const,
        };
      },
    }),
  ],

  // ── JWT callbacks ──────────────────────────────────────────────────────────
  callbacks: {
    async jwt({ token, user, account }) {
      // `user` is only populated on initial sign-in, not on subsequent
      // calls.  We persist the fields we need to the JWT so they survive
      // token refreshes.
      const augmentedToken = token as typeof token & { tenantId: string; provider: "credentials" | "saml" };
      if (user) {
        augmentedToken.tenantId = (user as { tenantId: string }).tenantId;
        augmentedToken.provider = account?.provider === "boxyhq-saml" ? "saml" : "credentials";
      }
      return augmentedToken;
    },

    async session({ session, token }) {
      // Expose tenantId and provider to client components via useSession()
      const augmentedToken = token as typeof token & { tenantId: string; provider: "credentials" | "saml" };
      session.user.tenantId = augmentedToken.tenantId;
      session.user.provider = augmentedToken.provider;
      return session;
    },
  },

  // ── Custom pages ──────────────────────────────────────────────────────────
  pages: {
    signIn: "/login",
    error:  "/login",   // Redirect auth errors back to login with ?error=
  },

  // ── Session strategy ──────────────────────────────────────────────────────
  // JWT is stateless – no session DB required.
  // For HIPAA / FedRAMP workloads, switch to "database" strategy.
  session: { strategy: "jwt" },
});
