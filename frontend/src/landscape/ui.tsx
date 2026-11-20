// Small shared pieces: translation context, status badges, the event stream.

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect } from "react";
import type { Translate } from "../lib/i18n";

export interface AppInfo {
  t: Translate;
  role: string;
  language: string;
}

export const AppContext = createContext<AppInfo>({ t: (k) => k, role: "viewer", language: "en" });

export const useApp = () => useContext(AppContext);

const BADGE: Record<string, string> = {
  GREEN: "green",
  YELLOW: "yellow",
  RED: "red",
  GRAY: "gray",
  UNKNOWN: "unknown",
};

export function StatusBadge({ status, prefix = "status" }: { status: string; prefix?: string }) {
  const { t } = useApp();
  return <span className={`lo-badge lo-badge-${BADGE[status] ?? "unknown"}`}>{t(`${prefix}.${status}`)}</span>;
}

export function StatusDot({ status, label }: { status: string; label?: string }) {
  return <span className={`lo-dot lo-dot-${BADGE[status] ?? "unknown"}`} role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true} />;
}

export function formatTime(value: string | null, language: string): string {
  if (!value) return "";
  const date = new Date(value);
  return date.toLocaleString(language === "de" ? "de-DE" : "en-GB", {
    dateStyle: "short",
    timeStyle: "short",
  });
}

/** Reloads the landscape and open process lists when the server reports a change. */
export function useLiveUpdates(): void {
  const queries = useQueryClient();
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const source = new EventSource("/api/events");
    const reload = () => {
      void queries.invalidateQueries({ queryKey: ["landscape"] });
    };
    source.addEventListener("change", reload);
    return () => source.close();
  }, [queries]);
}
