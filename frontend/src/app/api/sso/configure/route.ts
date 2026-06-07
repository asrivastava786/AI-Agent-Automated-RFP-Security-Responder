/**
 * app/api/sso/configure/route.ts – Server-side proxy for BoxyHQ Jackson admin API.
 *
 * The browser calls this Next.js route; this route calls Jackson using the
 * BOXYHQ_SAML_ADMIN_SECRET environment variable.  The secret never reaches
 * the browser.
 *
 * Supported methods: GET (list), POST (create/update), DELETE (remove)
 */

import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const JACKSON_BASE = process.env.BOXYHQ_SAML_ISSUER ?? "http://localhost:5225";
const ADMIN_SECRET = process.env.BOXYHQ_SAML_ADMIN_SECRET ?? "";

function jacksonHeaders() {
  return {
    "Content-Type": "application/json",
    Authorization:  `Bearer ${ADMIN_SECRET}`,
  };
}

// ── POST – create / update SSO config ─────────────────────────────────────────

export async function POST(req: NextRequest) {
  const session = await auth();
  if (!session) return NextResponse.json({ error: "Unauthorised" }, { status: 401 });

  const body = await req.json();

  const jacksonRes = await fetch(`${JACKSON_BASE}/api/v1/saml/config`, {
    method:  "POST",
    headers: jacksonHeaders(),
    body:    JSON.stringify(body),
  });

  const data = await jacksonRes.json();
  return NextResponse.json(data, { status: jacksonRes.status });
}

// ── GET – list SSO configs ────────────────────────────────────────────────────

export async function GET(req: NextRequest) {
  const session = await auth();
  if (!session) return NextResponse.json({ error: "Unauthorised" }, { status: 401 });

  const { searchParams } = new URL(req.url);
  const query = searchParams.toString();

  const jacksonRes = await fetch(
    `${JACKSON_BASE}/api/v1/saml/config${query ? `?${query}` : ""}`,
    { headers: jacksonHeaders() }
  );

  const data = await jacksonRes.json();
  return NextResponse.json(data, { status: jacksonRes.status });
}

// ── DELETE – remove SSO config ────────────────────────────────────────────────

export async function DELETE(req: NextRequest) {
  const session = await auth();
  if (!session) return NextResponse.json({ error: "Unauthorised" }, { status: 401 });

  const { searchParams } = new URL(req.url);
  const tenant  = searchParams.get("tenant");
  const product = searchParams.get("product");

  const jacksonRes = await fetch(
    `${JACKSON_BASE}/api/v1/saml/config?tenant=${tenant}&product=${product}`,
    { method: "DELETE", headers: jacksonHeaders() }
  );

  return NextResponse.json({}, { status: jacksonRes.status });
}
