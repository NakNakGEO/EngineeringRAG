import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "./api";

export interface ActivityEvent {
  event_id: string;
  run_id: string;
  run_kind: string;
  project_id: string | null;
  type: string;
  timestamp: string;
  status: string;
  summary: string;
  actor: string;
  data: { path?: string; change?: string } & Record<string, unknown>;
  goal: string;
}

export interface Live {
  /** newest first */
  events: ActivityEvent[];
  /** per project: path -> ms timestamp of the change, so the map can mark it */
  pulses: (projectId: string) => ReadonlyMap<string, number>;
  /** ISO time of the last completed sync per project */
  lastSync: ReadonlyMap<string, string>;
  online: boolean;
}

const EMPTY: Live = { events: [], pulses: () => new Map(), lastSync: new Map(), online: false };
const LiveContext = createContext<Live>(EMPTY);
export const useLive = (): Live => useContext(LiveContext);

const POLL_MS = 2000;
const KEEP = 250;
const FRESH_MS = 600_000; // events older than this at first sight are not marked as changed
const KEEP_CHANGED_MS = 600_000; // a changed file keeps its marker this long

/** One poller for the whole app: the feed on "Start here" and the glow on the graph share it. */
export function LiveProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [pulseState, setPulseState] = useState<Map<string, Map<string, number>>>(new Map());
  const newest = useRef<string | null>(null);
  const seen = useRef(new Set<string>());
  const first = useRef(true);

  const poll = useQuery({
    queryKey: ["activity"],
    queryFn: () => api<ActivityEvent[]>(`/activity?limit=150${newest.current ? `&after=${encodeURIComponent(newest.current)}` : ""}`),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: false,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });

  useEffect(() => {
    const batch = poll.data;
    if (!batch || batch.length === 0) {
      first.current = false;
      return;
    }
    const fresh = batch.filter((e) => !seen.current.has(e.event_id));
    if (fresh.length === 0) return;
    for (const e of fresh) seen.current.add(e.event_id);
    newest.current = fresh.reduce((m, e) => (e.timestamp > m ? e.timestamp : m), newest.current ?? "");
    setEvents((prev) => [...fresh, ...prev].sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1)).slice(0, KEEP));

    const now = Date.now();
    const touched = new Set<string>();
    setPulseState((prev) => {
      const next = new Map(prev);
      for (const e of fresh) {
        if (e.type !== "FILE_CHANGED" || !e.project_id || !e.data.path) continue;
        if (first.current && now - new Date(e.timestamp).getTime() > FRESH_MS) continue;
        const m = new Map(next.get(e.project_id) ?? []);
        m.set(e.data.path, now);
        next.set(e.project_id, m);
      }
      return next;
    });
    for (const e of fresh) {
      if (e.type === "PROJECT_SYNCED" && e.project_id && !touched.has(e.project_id)) {
        touched.add(e.project_id);
        for (const key of ["overview", "projects", "coverage", "allfiles", "notes", "file"]) void qc.invalidateQueries({ queryKey: [key] });
      }
    }
    first.current = false;
  }, [poll.data, qc]);

  // Forget finished pulses so the map does not grow forever.
  useEffect(() => {
    const t = setInterval(() => {
      const cutoff = Date.now() - KEEP_CHANGED_MS;
      setPulseState((prev) => {
        let changed = false;
        const next = new Map<string, Map<string, number>>();
        for (const [pid, m] of prev) {
          const keep = new Map([...m].filter(([, ts]) => ts > cutoff));
          if (keep.size !== m.size) changed = true;
          if (keep.size) next.set(pid, keep);
          else if (m.size) changed = true;
        }
        return changed ? next : prev;
      });
    }, 3000);
    return () => clearInterval(t);
  }, []);

  const value = useMemo<Live>(() => {
    const lastSync = new Map<string, string>();
    for (const e of events) if (e.type === "PROJECT_SYNCED" && e.project_id && !lastSync.has(e.project_id)) lastSync.set(e.project_id, e.timestamp);
    const empty = new Map<string, number>();
    return { events, pulses: (id) => pulseState.get(id) ?? empty, lastSync, online: !poll.isError };
  }, [events, pulseState, poll.isError]);

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}
