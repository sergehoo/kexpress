"use client";

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AuthProvider } from "@/lib/auth";
import { ThemeProvider } from "@/lib/theme";
import { SubsidiaryProvider } from "@/lib/subsidiary";

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <AuthProvider>
          <SubsidiaryProvider>{children}</SubsidiaryProvider>
        </AuthProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
