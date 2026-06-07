"use client";

/**
 * lib/providers.tsx – Client-side React context providers.
 *
 * Wraps the app with:
 *   • SessionProvider  – makes useSession() work in Client Components
 *   • QueryClientProvider – TanStack Query for data fetching + polling
 */

import { SessionProvider } from "next-auth/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { useState, type ReactNode } from "react";

export function Providers({ children }: { children: ReactNode }) {
  // One QueryClient per component tree.
  // useState ensures the client is not shared across SSR requests.
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Don't refetch on window focus for this B2B app –
            // reviewers keep the tab open for extended periods.
            refetchOnWindowFocus: false,
            // Stale time of 10s – balance freshness vs server load.
            staleTime: 10_000,
            retry: 2,
          },
        },
      })
  );

  return (
    <SessionProvider>
      <QueryClientProvider client={queryClient}>
        {children}
        {process.env.NODE_ENV === "development" && (
          <ReactQueryDevtools initialIsOpen={false} />
        )}
      </QueryClientProvider>
    </SessionProvider>
  );
}
