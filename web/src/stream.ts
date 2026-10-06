import { useEffect, useReducer, useRef } from "react";
import { BASE, type EventEnvelope } from "./api";
import { EVENT_TYPES } from "./eventTypes";

/** Bounded, de-duplicated, ordered event window. Memory stays flat however long a run is. */
export interface StreamState {
  events: EventEnvelope[]; // ascending by seq, at most `limit`
  lastSeq: number;
  dropped: number; // events evicted from the head (still available from the API)
  connection: "connecting" | "open" | "reconnecting" | "ended";
}

export const initialStream = (): StreamState => ({
  events: [],
  lastSeq: 0,
  dropped: 0,
  connection: "connecting",
});

export type StreamAction =
  | { kind: "events"; items: EventEnvelope[]; limit: number }
  | { kind: "connection"; value: StreamState["connection"] }
  | { kind: "reset" };

export function streamReducer(state: StreamState, action: StreamAction): StreamState {
  switch (action.kind) {
    case "reset":
      return initialStream();
    case "connection":
      return { ...state, connection: action.value };
    case "events": {
      const fresh = action.items.filter((e) => e.seq > state.lastSeq).sort((a, b) => a.seq - b.seq);
      if (fresh.length === 0) return state;
      const merged = [...state.events, ...fresh];
      const overflow = Math.max(0, merged.length - action.limit);
      return {
        ...state,
        events: overflow ? merged.slice(overflow) : merged,
        dropped: state.dropped + overflow,
        lastSeq: fresh[fresh.length - 1]!.seq,
      };
    }
  }
}

/** Live events of one run over SSE; resumes from the last seen seq after any disconnect. */
export function useRunStream(runId: string | null, limit = 2000): StreamState {
  const [state, dispatch] = useReducer(streamReducer, undefined, initialStream);
  const lastSeq = useRef(0);
  lastSeq.current = state.lastSeq;

  useEffect(() => {
    dispatch({ kind: "reset" });
    if (!runId) return;
    let closed = false;
    let source: EventSource | null = null;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (closed) return;
      source = new EventSource(`${BASE}/runs/${runId}/stream?after_seq=${lastSeq.current}`);
      source.onopen = () => {
        retry = 0;
        dispatch({ kind: "connection", value: "open" });
      };
      const onEvent = (msg: MessageEvent<string>) => {
        try {
          dispatch({ kind: "events", items: [JSON.parse(msg.data) as EventEnvelope], limit });
        } catch {
          /* ignore malformed frame */
        }
      };
      for (const type of EVENT_TYPES) source.addEventListener(type, onEvent as EventListener);
      source.addEventListener("end", () => {
        closed = true;
        source?.close();
        dispatch({ kind: "connection", value: "ended" });
      });
      source.onerror = () => {
        source?.close();
        if (closed) return;
        dispatch({ kind: "connection", value: "reconnecting" });
        retry = Math.min(retry + 1, 6);
        timer = setTimeout(connect, 500 * 2 ** retry);
      };
    };
    connect();
    return () => {
      closed = true;
      source?.close();
      if (timer) clearTimeout(timer);
    };
  }, [runId, limit]);

  return state;
}
