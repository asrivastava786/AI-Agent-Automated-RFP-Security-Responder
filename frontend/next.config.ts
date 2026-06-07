import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Produces a self-contained server bundle for Docker (copies only what the
  // server needs, skipping devDependencies).
  output: "standalone",

  // Proxy all /api/rfp/* calls to the FastAPI backend so the frontend
  // never has to hard-code the backend origin in client-side code.
  // NEXT_PUBLIC_API_URL is only used server-side in lib/api.ts.
  async rewrites() {
    return [
      {
        source: "/api/rfp/:path*",
        destination: `${process.env.BACKEND_URL ?? "http://localhost:8000"}/api/v1/rfp/:path*`,
      },
    ];
  },

  // Strict mode catches double-render bugs early in dev.
  reactStrictMode: true,
};

export default nextConfig;
