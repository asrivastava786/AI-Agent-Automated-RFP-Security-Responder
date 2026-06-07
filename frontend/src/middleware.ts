/**
 * middleware.ts – Route protection via NextAuth v5 session check.
 *
 * Rules
 * ─────
 * /login        → public (always accessible)
 * /api/auth/*   → public (NextAuth handler routes)
 * /api/rfp/*    → protected (Next.js rewrites to FastAPI; session required)
 * everything else → protected; unauthenticated requests are redirected to /login
 *
 * The `auth` export from lib/auth.ts doubles as middleware when exported here.
 * NextAuth v5 checks the JWT cookie automatically on every matched request.
 */

import { auth } from "@/lib/auth";
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export default auth(async (req) => {
  const { nextUrl, auth: session } = req as NextRequest & { auth: unknown };

  const isLoggedIn   = !!session;
  const isAuthRoute  = nextUrl.pathname.startsWith("/api/auth");
  const isPublicPage = nextUrl.pathname === "/login";

  if (isAuthRoute || isPublicPage) return NextResponse.next();

  if (!isLoggedIn) {
    const loginUrl = new URL("/login", nextUrl.origin);
    loginUrl.searchParams.set("callbackUrl", nextUrl.pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
});

export const config = {
  matcher: [
    // Skip Next.js internals and static assets
    "/((?!_next/static|_next/image|favicon.ico).*)",
  ],
};
