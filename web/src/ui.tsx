import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef, type ReactNode } from "react";

export function Badge({ value }: { value: string | null | undefined }) {
  const v = (value ?? "—").toString();
  const tone = /fail|deny|denied|broken|quarantine|contradict|stale|reject/i.test(v)
    ? "bad"
    : /complete|allow|ok|trusted|verified|current|selected|passed|approved|accepted/i.test(v)
      ? "good"
      : /pending|waiting|require|experimental|running|started|proposed|unverified/i.test(v)
        ? "warn"
        : "";
  return <span className={`badge ${tone}`}>{v}</span>;
}

export function Panel({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="panel">
      <header>
        <h3>{title}</h3>
        {actions}
      </header>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function ErrorNote({ error }: { error: unknown }) {
  return error ? <p className="error">{error instanceof Error ? error.message : String(error)}</p> : null;
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="stat">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

/** Virtualised list: only visible rows are in the DOM, so 100k rows cost the same as 20. */
export function VirtualList<T>({
  items, rowHeight = 30, height = 420, render, keyOf,
}: {
  items: T[]; rowHeight?: number; height?: number; render: (item: T) => ReactNode; keyOf: (item: T) => string;
}) {
  const parent = useRef<HTMLDivElement>(null);
  const v = useVirtualizer({ count: items.length, getScrollElement: () => parent.current, estimateSize: () => rowHeight, overscan: 12 });
  return (
    <div ref={parent} className="vlist" style={{ height }}>
      <div style={{ height: v.getTotalSize(), position: "relative" }}>
        {v.getVirtualItems().map((row) => {
          const item = items[row.index]!;
          return (
            <div key={keyOf(item)} className="vrow" style={{ height: rowHeight, transform: `translateY(${row.start}px)` }}>
              {render(item)}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleTimeString() : "—");
