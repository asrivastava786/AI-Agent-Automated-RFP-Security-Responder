/**
 * app/api/auth/[...nextauth]/route.ts
 *
 * NextAuth v5 catch-all route handler.
 * Exposes GET and POST on /api/auth/* – covers:
 *   /api/auth/signin          – initiates sign-in
 *   /api/auth/signout         – initiates sign-out
 *   /api/auth/callback/*      – OAuth/SAML callback handler
 *   /api/auth/session         – session retrieval (used by useSession)
 *   /api/auth/csrf            – CSRF token
 */

import { handlers } from "@/lib/auth";

export const { GET, POST } = handlers;
