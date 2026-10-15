/** Thin adapters over the shadcn-style components, shared by the operational views
 * (Mission Control, Capabilities, Workshop, Security, Observability, Projects, Run Graph). */
import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { Badge as BaseBadge, toneOf } from "./components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./components/ui/card";
import { EmptyState, ErrorNote, Stat } from "./components/ui/misc";
import { cn } from "./lib/utils";

export { ErrorNote, Stat };

/** A status word coloured by meaning (completed, failed, DIRTY, OBSERVED …). */
export function Badge({ value }: { value: string | null | undefined }) {
  const v = (value ?? "—").toString();
  return <BaseBadge tone={toneOf(v)} dot>{v}</BaseBadge>;
}

export function Panel({
  title, description, actions, children, className,
}: { title: string; description?: string; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <Card className={className}>
      <CardHeader>
        <div className="min-w-0">
          <CardTitle>{title}</CardTitle>
          {description && <CardDescription>{description}</CardDescription>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </CardHeader>
      <CardContent className="space-y-3">{children}</CardContent>
    </Card>
  );
}

export const Empty = ({ children }: { children: ReactNode }) => <EmptyState>{children}</EmptyState>;

/** Virtualised list: only visible rows are in the DOM, so 100k rows cost the same as 20.
 * With `onSelect` the whole row is the click target and is keyboard operable. */
export function VirtualList<T>({
  items, rowHeight = 40, height = 420, render, keyOf, onSelect, selectedKey,
}: {
  items: T[]; rowHeight?: number; height?: number; render: (item: T) => ReactNode; keyOf: (item: T) => string;
  onSelect?: (item: T) => void; selectedKey?: string | null;
}) {
  const parent = useRef<HTMLDivElement>(null);
  const v = useVirtualizer({ count: items.length, getScrollElement: () => parent.current, estimateSize: () => rowHeight, overscan: 12 });
  return (
    <div ref={parent} className="overflow-auto rounded-lg border" style={{ height }}>
      <div style={{ height: v.getTotalSize(), position: "relative" }}>
        {v.getVirtualItems().map((row) => {
          const item = items[row.index]!;
          const key = keyOf(item);
          const selected = onSelect !== undefined && key === selectedKey;
          return (
            <div
              key={key}
              className={cn(
                "absolute inset-x-0 flex items-center gap-2 overflow-hidden border-b px-3 text-[13px] whitespace-nowrap",
                onSelect && "cursor-pointer hover:bg-accent/60",
                selected && "bg-accent shadow-[inset_3px_0_0_var(--primary)]",
              )}
              style={{ height: rowHeight, transform: `translateY(${row.start}px)` }}
              {...(onSelect && {
                role: "button",
                tabIndex: 0,
                "aria-pressed": selected,
                onClick: () => onSelect(item),
                onKeyDown: (e: KeyboardEvent<HTMLDivElement>) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(item);
                  }
                },
              })}
            >
              {render(item)}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleTimeString() : "—");
