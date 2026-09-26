import { useCallback, useEffect, useState } from "react";

// Hash routing (no dependency, no server SPA fallback needed): every view is a
// shareable URL such as `#/logs?request_id=abc` or `#/clients/<id>`, and the
// browser's back/forward buttons move between views because each navigation is
// a history entry.

export interface Route {
  view: string;
  segments: string[];
  params: URLSearchParams;
}

export function parseHash(hash: string, fallback = "overview"): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [pathPart, query = ""] = raw.split("?", 2);
  let parts: string[];
  try {
    parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  } catch (error) {
    if (!(error instanceof URIError)) throw error;
    // Treat the whole malformed path as unavailable, including bad client IDs.
    // Keep the shell usable so the operator can navigate away without reloading.
    return { view: pathPart, segments: [], params: new URLSearchParams(query) };
  }
  return {
    view: parts[0] ?? fallback,
    segments: parts.slice(1),
    params: new URLSearchParams(query),
  };
}

export function buildHash(
  view: string,
  opts: { segments?: string[]; params?: Record<string, string | undefined> } = {},
): string {
  const path = [view, ...(opts.segments ?? [])].map(encodeURIComponent).join("/");
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(opts.params ?? {})) {
    if (v) q.set(k, v);
  }
  const qs = q.toString();
  return `#/${path}${qs ? `?${qs}` : ""}`;
}

export function useHashRoute(fallback = "overview"): [Route, (hash: string) => void] {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash, fallback));

  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash, fallback));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, [fallback]);

  const navigate = useCallback(
    (hash: string) => {
      if (window.location.hash === hash) return;
      // Assigning the hash adds a history entry and fires `hashchange`.
      window.location.hash = hash;
      setRoute(parseHash(hash, fallback));
    },
    [fallback],
  );

  return [route, navigate];
}
