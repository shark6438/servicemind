"use client";

import useSWR, { type SWRConfiguration } from "swr";
import type { ZodType } from "zod";
import { apiRequest } from "@/lib/api";
import { useAuth } from "@/providers/auth-provider";

export function useApi<T>(path: string | null, schema: ZodType<T>, config?: SWRConfiguration<T>) {
  const { authenticated, getToken } = useAuth();
  return useSWR<T>(authenticated ? path : null, (key: string) => apiRequest(key, getToken, schema), {
    revalidateOnFocus: true, shouldRetryOnError: false, ...config,
  });
}
