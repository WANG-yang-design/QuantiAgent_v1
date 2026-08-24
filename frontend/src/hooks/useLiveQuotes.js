import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export const LIVE_QUOTE_INTERVAL = 3000;

export function normalizeSymbols(symbols = []) {
  return [...new Set(symbols.map((s) => String(s || "").trim().toUpperCase()).filter(Boolean))].sort();
}

/**
 * One shared query per symbol set.  It remains fresh in the background and all
 * pages consume the same cache, so navigating to a detail page cannot freeze a
 * list page at the value it had when it was unmounted.
 */
export function useLiveQuotes(symbols, options = {}) {
  const key = useMemo(() => normalizeSymbols(symbols), [JSON.stringify(symbols || [])]);
  return useQuery({
    queryKey: ["live-quotes", key.join(",")],
    queryFn: () => api.get("/api/quotes", { symbols: key.join(","), limit: Math.max(100, key.length) }),
    enabled: key.length > 0,
    staleTime: 1000,
    refetchInterval: LIVE_QUOTE_INTERVAL,
    refetchIntervalInBackground: true,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
    ...options,
  });
}

export function toQuoteMap(data) {
  return Object.fromEntries((data?.quotes || []).map((q) => [q.symbol, q]));
}
